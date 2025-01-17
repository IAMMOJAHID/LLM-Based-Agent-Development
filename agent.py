#!/usr/bin/env python
# coding=utf-8

# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .. import is_torch_available
from ..utils import logging as transformers_logging
from ..utils.import_utils import is_pygments_available
from .agent_types import AgentAudio, AgentImage, AgentText
from .default_tools import BASE_PYTHON_TOOLS, FinalAnswerTool, setup_default_tools
from .llm_engine import HfEngine, MessageRole
from .prompts import (
    DEFAULT_CODE_SYSTEM_PROMPT,
    DEFAULT_REACT_CODE_SYSTEM_PROMPT,
    DEFAULT_REACT_JSON_SYSTEM_PROMPT,
    PLAN_UPDATE_FINAL_PLAN_REDACTION,
    SYSTEM_PROMPT_FACTS,
    SYSTEM_PROMPT_FACTS_UPDATE,
    SYSTEM_PROMPT_PLAN,
    SYSTEM_PROMPT_PLAN_UPDATE,
    USER_PROMPT_FACTS_UPDATE,
    USER_PROMPT_PLAN,
    USER_PROMPT_PLAN_UPDATE,
)
from .python_interpreter import LIST_SAFE_MODULES, evaluate_python_code
from .tools import (
    DEFAULT_TOOL_DESCRIPTION_TEMPLATE,
    Tool,
    get_tool_description_with_args,
    load_tool,
)


if is_pygments_available():
    from pygments import highlight
    from pygments.formatters import Terminal256Formatter
    from pygments.lexers import PythonLexer


class CustomFormatter(logging.Formatter):
    grey = "\x1b[38;20m"
    bold_yellow = "\x1b[33;1m"
    red = "\x1b[31;20m"
    green = "\x1b[32;20m"
    bold_red = "\x1b[31;1m"
    bold_white = "\x1b[37;1m"
    reset = "\x1b[0m"
    format = "%(message)s"

    FORMATS = {
        logging.DEBUG: grey + format + reset,
        logging.INFO: format,
        logging.WARNING: bold_yellow + format + reset,
        31: reset + format + reset,
        32: green + format + reset,
        33: bold_white + format + reset,
        logging.ERROR: red + format + reset,
        logging.CRITICAL: bold_red + format + reset,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


logger = transformers_logging.get_logger(__name__)
logger.propagate = False
ch = logging.StreamHandler()
ch.setFormatter(CustomFormatter())
logger.addHandler(ch)


def parse_json_blob(json_blob: str) -> Dict[str, str]:
    try:
        first_accolade_index = json_blob.find("{")
        last_accolade_index = [a.start() for a in list(re.finditer("}", json_blob))][-1]
        json_blob = json_blob[first_accolade_index : last_accolade_index + 1].replace('\\"', "'")
        json_data = json.loads(json_blob, strict=False)
        return json_data
    except json.JSONDecodeError as e:
        place = e.pos
        if json_blob[place - 1 : place + 2] == "},\n":
            raise ValueError(
                "JSON is invalid: you probably tried to provide multiple tool calls in one action. PROVIDE ONLY ONE TOOL CALL."
            )
        raise ValueError(
            f"The JSON blob you used is invalid due to the following error: {e}.\n"
            f"JSON blob was: {json_blob}, decoding failed on that specific part of the blob:\n"
            f"'{json_blob[place-4:place+5]}'."
        )
    except Exception as e:
        raise ValueError(f"Error in parsing the JSON blob: {e}")


def parse_code_blob(code_blob: str) -> str:
    try:
        pattern = r"```(?:py|python)?\n(.*?)\n```"
        match = re.search(pattern, code_blob, re.DOTALL)
        return match.group(1).strip()
    except Exception as e:
        raise ValueError(
            f"""
The code blob you used is invalid: due to the following error: {e}
This means that the regex pattern {pattern} was not respected: make sure to include code with the correct pattern, for instance:
Thoughts: Your thoughts
Code:
```py
# Your python code here
```<end_action>"""
        )


def parse_json_tool_call(json_blob: str) -> Tuple[str, Dict[str, str]]:
    json_blob = json_blob.replace("```json", "").replace("```", "")
    tool_call = parse_json_blob(json_blob)
    if "action" in tool_call and "action_input" in tool_call:
        return tool_call["action"], tool_call["action_input"]
    elif "action" in tool_call:
        return tool_call["action"], None
    else:
        raise ValueError(
            f"Missing keys: {[key for key in ['action', 'action_input'] if key not in tool_call]} in blob {tool_call}"
        )


def parse_text_tool_call(text: str) -> Tuple[str, Union[str, Dict[str, str]]]:
    """
    Expects a text in the format: 'Action:', 'Action input:', 'Observation:'. 'Action input:' contains a json string with input arguments.
    """
    try:
        if "Observation:" in text:
            text = text.split("Observation:")[0]
        if "Action:" in text:
            text = text.split("Action:")[1]
        tool_name, tool_input = text.split("Action input:")
        if "{" in tool_input:
            tool_input = parse_json_blob(tool_input)
        else:
            tool_input = tool_input.strip().replace('"', "")
        return tool_name.strip().replace('"', "").replace("\\", ""), tool_input
    except Exception as e:
        raise ValueError(
            f"Error in parsing the text tool call: {e}. Be sure to provide the correct format. DO NOT repeat your previous incorrect tool call."
        )


def to_text(input: Union[List[Dict[str, str]], Dict[str, str], str]) -> str:
    if isinstance(input, list):
        return "\n".join([m["content"] for m in input])
    elif isinstance(input, dict):
        return input["content"]
    else:
        return input


HUGGINGFACE_DEFAULT_TOOLS = {}
_tools_are_initialized = False


class Toolbox:
    """
    The toolbox contains all tools that the agent can perform operations with, as well as a few methods to
    manage them.

    Args:
        tools (`List[Tool]`):
            The list of tools to instantiate the toolbox with
        add_base_tools (`bool`, defaults to `False`, *optional*, defaults to `False`):
            Whether to add the tools available within `transformers` to the toolbox.
    """

    def __init__(self, tools: List[Tool], add_base_tools: bool = False):
        self._tools = {tool.name: tool for tool in tools}
        if add_base_tools:
            self.add_base_tools()
        self._load_tools_if_needed()

    def add_base_tools(self, add_python_interpreter: bool = False):
        global _tools_are_initialized
        global HUGGINGFACE_DEFAULT_TOOLS
        if not _tools_are_initialized:
            HUGGINGFACE_DEFAULT_TOOLS = setup_default_tools(logger)
            _tools_are_initialized = True
        for tool in HUGGINGFACE_DEFAULT_TOOLS.values():
            if tool.name != "python_interpreter" or add_python_interpreter:
                self.add_tool(tool)
        self._load_tools_if_needed()

    @property
    def tools(self) -> Dict[str, Tool]:
        """Get all tools currently in the toolbox"""
        return self._tools

    def show_tool_descriptions(self, tool_description_template: str = None) -> str:
        """
        Returns the description of all tools in the toolbox

        Args:
            tool_description_template (`str`, *optional*):
                The template to use to describe the tools. If not provided, the default template will be used.
        """
        return "\n".join(
            [get_tool_description_with_args(tool, tool_description_template) for tool in self._tools.values()]
        )

    def add_tool(self, tool: Tool):
        """
        Adds a tool to the toolbox

        Args:
            tool (`Tool`):
                The tool to add to the toolbox.
        """
        if tool.name in self._tools:
            raise KeyError(f"Error: tool '{tool.name}' already exists in the toolbox.")
        self._tools[tool.name] = tool

    def remove_tool(self, tool_name: str):
        """
        Removes a tool from the toolbox

        Args:
            tool_name (`str`):
                The tool to remove from the toolbox.
        """
        if tool_name not in self._tools:
            raise KeyError(
                f"Error: tool {tool_name} not found in toolbox for removal, should be instead one of {list(self._tools.keys())}."
            )
        del self._tools[tool_name]

    def update_tool(self, tool: Tool):
        """
        Updates a tool in the toolbox according to its name.

        Args:
            tool (`Tool`):
                The tool to update to the toolbox.
        """
        if tool.name not in self._tools:
            raise KeyError(
                f"Error: tool {tool.name} not found in toolbox for update, should be instead one of {list(self._tools.keys())}."
            )
        self._tools[tool.name] = tool

    def clear_toolbox(self):
        """Clears the toolbox"""
        self._tools = {}

    def _load_tools_if_needed(self):
        for name, tool in self._tools.items():
            if not isinstance(tool, Tool):
                task_or_repo_id = tool.task if tool.repo_id is None else tool.repo_id
                self._tools[name] = load_tool(task_or_repo_id)

    def __repr__(self):
        toolbox_description = "Toolbox contents:\n"
        for tool in self._tools.values():
            toolbox_description += f"\t{tool.name}: {tool.description}\n"
        return toolbox_description


class AgentError(Exception):
    """Base class for other agent-related exceptions"""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class AgentParsingError(AgentError):
    """Exception raised for errors in parsing in the agent"""

    pass


class AgentExecutionError(AgentError):
    """Exception raised for errors in execution in the agent"""

    pass


class AgentMaxIterationsError(AgentError):
    """Exception raised for errors in execution in the agent"""

    pass


class AgentGenerationError(AgentError):
    """Exception raised for errors in generation in the agent"""

    pass


def format_prompt_with_tools(toolbox: Toolbox, prompt_template: str, tool_description_template: str) -> str:
    tool_descriptions = toolbox.show_tool_descriptions(tool_description_template)
    prompt = prompt_template.replace("<<tool_descriptions>>", tool_descriptions)
    if "<<tool_names>>" in prompt:
        tool_names = [f"'{tool_name}'" for tool_name in toolbox.tools.keys()]
        prompt = prompt.replace("<<tool_names>>", ", ".join(tool_names))
    return prompt


def format_prompt_with_imports(prompt_template: str, authorized_imports: List[str]) -> str:
    if "<<authorized_imports>>" not in prompt_template:
        raise AgentError("Tag '<<authorized_imports>>' should be provided in the prompt.")
    return prompt_template.replace("<<authorized_imports>>", str(authorized_imports))


class Agent:
    def __init__(
        self,
        tools: Union[List[Tool], Toolbox],
        llm_engine: Callable = HfEngine(),
        system_prompt=DEFAULT_REACT_JSON_SYSTEM_PROMPT,
        tool_description_template=None,
        additional_args={},
        max_iterations: int = 6,
        tool_parser=parse_json_tool_call,
        add_base_tools: bool = False,
        verbose: int = 0,
        memory_verbose: bool = False,
    ):
        self.agent_name = self.__class__.__name__
        self.llm_engine = llm_engine
        self.system_prompt_template = system_prompt
        self.tool_description_template = (
            tool_description_template if tool_description_template else DEFAULT_TOOL_DESCRIPTION_TEMPLATE
        )
        self.additional_args = additional_args
        self.max_iterations = max_iterations
        self.logger = logger
        self.tool_parser = tool_parser

        if isinstance(tools, Toolbox):
            self._toolbox = tools
            if add_base_tools:
                if not is_torch_available():
                    raise ImportError("Using the base tools requires torch to be installed.")

                self._toolbox.add_base_tools(add_python_interpreter=(self.__class__ == ReactJsonAgent))
        else:
            self._toolbox = Toolbox(tools, add_base_tools=add_base_tools)
        self._toolbox.add_tool(FinalAnswerTool())

        self.system_prompt = format_prompt_with_tools(
            self._toolbox, self.system_prompt_template, self.tool_description_template
        )
        self.prompt = None
        self.logs = []
        self.task = None
        self.memory_verbose = memory_verbose

        if verbose == 0:
            logger.setLevel(logging.WARNING)
        elif verbose == 1:
            logger.setLevel(logging.INFO)
        elif verbose == 2:
            logger.setLevel(logging.DEBUG)

    @property
    def toolbox(self) -> Toolbox:
        """Get the toolbox currently available to the agent"""
        return self._toolbox

    def initialize_for_run(self):
        self.token_count = 0
        self.system_prompt = format_prompt_with_tools(
            self._toolbox,
            self.system_prompt_template,
            self.tool_description_template,
        )
        if hasattr(self, "authorized_imports"):
            self.system_prompt = format_prompt_with_imports(
                self.system_prompt, list(set(LIST_SAFE_MODULES) | set(self.authorized_imports))
            )
        self.logs = [{"system_prompt": self.system_prompt, "task": self.task}]
        self.logger.warn("======== New task ========")
        self.logger.log(33, self.task)
        self.logger.debug("System prompt is as follows:")
        self.logger.debug(self.system_prompt)

    def write_inner_memory_from_logs(self, summary_mode: Optional[bool] = False) -> List[Dict[str, str]]:
        """
        Reads past llm_outputs, actions, and observations or errors from the logs into a series of messages
        that can be used as input to the LLM.
        """
        prompt_message = {"role": MessageRole.SYSTEM, "content": self.logs[0]["system_prompt"]}
        task_message = {
            "role": MessageRole.USER,
            "content": "Task: " + self.logs[0]["task"],
        }
        if summary_mode:
            memory = [task_message]
        else:
            memory = [prompt_message, task_message]
        for i, step_log in enumerate(self.logs[1:]):
            if "llm_output" in step_log and not summary_mode:
                thought_message = {"role": MessageRole.ASSISTANT, "content": step_log["llm_output"].strip()}
                memory.append(thought_message)
            if "facts" in step_log:
                thought_message = {
                    "role": MessageRole.ASSISTANT,
                    "content": "[FACTS LIST]:\n" + step_log["facts"].strip(),
                }
                memory.append(thought_message)

            if "plan" in step_log and not summary_mode:
                thought_message = {"role": MessageRole.ASSISTANT, "content": "[PLAN]:\n" + step_log["plan"].strip()}
                memory.append(thought_message)

            if "tool_call" in step_log and summary_mode:
                tool_call_message = {
                    "role": MessageRole.ASSISTANT,
                    "content": f"[STEP {i} TOOL CALL]: " + str(step_log["tool_call"]).strip(),
                }
                memory.append(tool_call_message)

            if "task" in step_log:
                tool_call_message = {
                    "role": MessageRole.USER,
                    "content": "New task:\n" + step_log["task"],
                }
                memory.append(tool_call_message)

            if "error" in step_log or "observation" in step_log:
                if "error" in step_log:
                    message_content = (
                        f"[OUTPUT OF STEP {i}] Error: "
                        + str(step_log["error"])
                        + "\nNow let's retry: take care not to repeat previous errors! If you have retried several times, try a completely different approach.\n"
                    )
                elif "observation" in step_log:
                    message_content = f"[OUTPUT OF STEP {i}] Observation:\n{step_log['observation']}"
                tool_response_message = {"role": MessageRole.TOOL_RESPONSE, "content": message_content}
                memory.append(tool_response_message)

        return memory

    def get_succinct_logs(self):
        return [{key: value for key, value in log.items() if key != "agent_memory"} for log in self.logs]

    def extract_action(self, llm_output: str, split_token: str) -> str:
        """
        Parse action from the LLM output

        Args:
            llm_output (`str`): Output of the LLM
            split_token (`str`): Separator for the action. Should match the example in the system prompt.
        """
        try:
            split = llm_output.split(split_token)
            rationale, action = (
                split[-2],
                split[-1],
            )  # NOTE: using indexes starting from the end solves for when you have more than one split_token in the output
        except Exception as e:
            self.logger.error(e, exc_info=1)
            raise AgentParsingError(
                f"Error: No '{split_token}' token provided in your output.\nYour output:\n{llm_output}\n. Be sure to include an action, prefaced with '{split_token}'!"
            )
        return rationale, action

    def execute_tool_call(self, tool_name: str, arguments: Dict[str, str]) -> Any:
        """
        Execute tool with the provided input and returns the result.
        This method replaces arguments with the actual values from the state if they refer to state variables.

        Args:
            tool_name (`str`): Name of the Tool to execute (should be one from self.toolbox).
            arguments (Dict[str, str]): Arguments passed to the Tool.
        """
        if tool_name not in self.toolbox.tools:
            error_msg = f"Error: unknown tool {tool_name}, should be instead one of {list(self.toolbox.tools.keys())}."
            self.logger.error(error_msg, exc_info=1)
            raise AgentExecutionError(error_msg)

        try:
            if isinstance(arguments, str):
                observation = self.toolbox.tools[tool_name](arguments)
            else:
                for key, value in arguments.items():
                    # if the value is the name of a state variable like "image.png", replace it with the actual value
                    if isinstance(value, str) and value in self.state:
                        arguments[key] = self.state[value]
                observation = self.toolbox.tools[tool_name](**arguments)
            return observation
        except Exception as e:
            raise AgentExecutionError(
                f"Error in tool call execution: {e}\nYou should only use this tool with a correct input.\n"
                f"As a reminder, this tool's description is the following:\n{get_tool_description_with_args(self.toolbox.tools[tool_name])}"
            )

    def log_code_action(self, code_action: str) -> None:
        self.logger.warning("==== Agent is executing the code below:")
        if is_pygments_available():
            self.logger.log(
                31, highlight(code_action, PythonLexer(ensurenl=False), Terminal256Formatter(style="nord"))
            )
        else:
            self.logger.log(31, code_action)
        self.logger.warning("====")

    def run(self, **kwargs):
        """To be implemented in the child class"""
        raise NotImplementedError


class CodeAgent(Agent):
    """
    A class for an agent that solves the given task using a single block of code. It plans all its actions, then executes all in one shot.
    """

    def __init__(
        self,
        tools: List[Tool],
        llm_engine: Callable = HfEngine(),
        system_prompt: str = DEFAULT_CODE_SYSTEM_PROMPT,
        tool_description_template: str = DEFAULT_TOOL_DESCRIPTION_TEMPLATE,
        additional_authorized_imports: Optional[List[str]] = None,
        **kwargs,
    ):
        super().__init__(
            tools=tools,
            llm_engine=llm_engine,
            system_prompt=system_prompt,
            tool_description_template=tool_description_template,
            **kwargs,
        )

        if not is_pygments_available():
            transformers_logging.warning_once(
                logger,
                "pygments isn't installed. Installing pygments will enable color syntax highlighting in the "
                "CodeAgent.",
            )

        self.python_evaluator = evaluate_python_code
        self.additional_authorized_imports = additional_authorized_imports if additional_authorized_imports else []
        self.authorized_imports = list(set(LIST_SAFE_MODULES) | set(self.additional_authorized_imports))
        self.system_prompt = self.system_prompt.replace("<<authorized_imports>>", str(self.authorized_imports))

    def parse_code_blob(self, result: str) -> str:
        """
        Override this method if you want to change the way the code is
        cleaned in the `run` method.
        """
        return parse_code_blob(result)

    def run(self, task: str, return_generated_code: bool = False, **kwargs):
        """
        Runs the agent for the given task.

        Args:
            task (`str`): The task to perform
            return_generated_code (`bool`, *optional*, defaults to `False`): Whether to return the generated code instead of running it
            kwargs (additional keyword arguments, *optional*):
                Any keyword argument to send to the agent when evaluating the code.

        Example:

        ```py
        from transformers.agents import CodeAgent, PythonInterpreterTool

        python_interpreter = PythonInterpreterTool()
        agent = CodeAgent(tools=[python_interpreter])
        agent.run("What is the result of 2 power 3.7384?")
        ```
        """
        self.task = task
        if len(kwargs) > 0:
            self.task += f"\nYou have been provided with these initial arguments: {str(kwargs)}."
        self.state = kwargs.copy()
        self.initialize_for_run()

        # Run LLM
        prompt_message = {"role": MessageRole.SYSTEM, "content": self.system_prompt}
        task_message = {
            "role": MessageRole.USER,
            "content": "Task: " + self.task,
        }

        self.prompt = [prompt_message, task_message]
        self.logger.info("====Executing with this prompt====")
        self.logger.info(self.prompt)
        llm_output = self.llm_engine(self.prompt, stop_sequences=["<end_action>"])

        if return_generated_code:
            return llm_output

        # Parse
        try:
            _, code_action = self.extract_action(llm_output=llm_output, split_token="Code:")
        except Exception as e:
            self.logger.debug(
                f"Error in extracting action, trying to parse the whole output as code. Error trace: {e}"
            )
            code_action = llm_output

        try:
            code_action = self.parse_code_blob(code_action)
        except Exception as e:
            error_msg = f"Error in code parsing: {e}. Be sure to provide correct code"
            self.logger.error(error_msg, exc_info=1)
            return error_msg

        # Execute
        self.log_code_action(code_action)
        try:
            available_tools = {**BASE_PYTHON_TOOLS.copy(), **self.toolbox.tools}
            output = self.python_evaluator(
                code_action,
                static_tools=available_tools,
                custom_tools={},
                state=self.state,
                authorized_imports=self.authorized_imports,
            )
            self.logger.info(self.state["print_outputs"])
            return output
        except Exception as e:
            error_msg = f"Error in execution: {e}. Be sure to provide correct code."
            self.logger.error(error_msg, exc_info=1)
            return error_msg