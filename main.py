from transformers import HfEngine, ReactCodeAgent
from huggingface_hub import login, InferenceClient
import pytesseract
from transformers.agents import CodeAgent, PythonInterpreterTool
from prompts import (
    DEFAULT_CODE_SYSTEM_PROMPT,
)

login("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")
pytesseract.pytesseract.tesseract_cmd = r'd:\hugging face model\agent\.venv\lib\site-packages'
agent = ReactCodeAgent(tools=[], system_prompt=DEFAULT_CODE_SYSTEM_PROMPT, add_base_tools=True , additional_authorized_imports=['requests', 'bs4', 'pygame', 'PIL', 'numpy', 'pytesseract', 'nltk', 'os'])
ans=agent.run(task="Read the image whose path store in `image_path` using PIL and convert it to RGB formate and then answer the question store in `question`", 
              image_path="bill.png",
              question="Whom this bill is for?",
              return_generated_code=False,
              )
# ans=agent.run("Could you summarize the page at url 'https://medium.com/@mojahid.iitdelhi/image-processing-vs-image-analysis-vs-computer-vision-8c46a30605e9'?")
# ans=agent.run("Generate an image of forest")
# ans=agent.run("Which country has the highest population: India or Pakistan?")
# ans=agent.run("What is the current age of the Shahrukh Khan(search the age first), raised to the power 0.36")
# ans=agent.run( "Summarize the text in the variable `text` using summarizer model and print it.",
#         text = "Khan began his career with appearances in several television series in the late 1980s and made his Bollywood debut in 1992 with the musical romance Deewana.He was initially recognised for playing villainous roles in the films Baazigar (1993) and Darr (1993). Khan established himself by starring in a series of top-grossing romantic films, including Dilwale Dulhania Le Jayenge (1995), Dil To Pagal Hai (1997), Kuch Kuch Hota Hai (1998), Mohabbatein (2000), Kabhi Khushi Kabhie Gham... (2001), Kal Ho Naa Ho (2003), Veer-Zaara (2004) and Kabhi Alvida Naa Kehna (2006). He earned critical acclaim for his portrayal of an alcoholic in the period romantic drama Devdas (2002), a NASA scientist in the social drama Swades (2004), a hockey coach in the sports drama Chak De! India (2007), and a man with Asperger syndrome in the drama My Name Is Khan (2010). Further commercial successes came with the romances Om Shanti Om (2007) and Rab Ne Bana Di Jodi (2008), and with his expansion to comedies in Chennai Express (2013) and Happy New Year (2014). Following a brief setback and hiatus, Khan made a career comeback with the 2023 action thrillers Pathaan and Jawan, both of which rank among the highest-grossing Indian films."
#     )
print("Ans:", ans)
# print("Track calling:",agent.system_prompt_template)
