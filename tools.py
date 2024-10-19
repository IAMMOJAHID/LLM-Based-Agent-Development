from typing import Dict
import importlib.util
from dataclasses import dataclass

TASK_MAPPING = {
    "document-question-answering": "DocumentQuestionAnsweringTool",
    "image-question-answering": "ImageQuestionAnsweringTool",
    "speech-to-text": "SpeechToTextTool",
    "text-to-speech": "TextToSpeechTool",
    "translation": "TranslationTool",
    "python_interpreter": "PythonInterpreterTool",
}


@dataclass
class PreTool:
    name: str
    inputs: Dict[str, str]
    output_type: type
    task: str
    description: str
    repo_id: str


def setup_default_tools():
    default_tools = {}
    main_module = importlib.import_module("transformers")
    tools_module = main_module.agents

    for task_name, tool_class_name in TASK_MAPPING.items():
        tool_class = getattr(tools_module, tool_class_name)
        tool_instance = tool_class()
        default_tools[tool_class.name] = PreTool(
            name=tool_instance.name,
            inputs=tool_instance.inputs,
            output_type=tool_instance.output_type,
            task=task_name,
            description=tool_instance.description,
            repo_id=None,
        )

    return default_tools

# ans=setup_default_tools()
# items = list(ans.items())
# print(items[4])
# print(".............................................................................")
# print(items[5])
