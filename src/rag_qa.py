import os
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_huggingface import HuggingFacePipeline
from langchain.chains import LLMChain
from langchain.prompts import PromptTemplate
from langchain.schema.output_parser import StrOutputParser
from langchain_core.runnables import RunnableLambda, RunnableMap
from langchain_core.output_parsers import StrOutputParser
from transformers import pipeline, AutoTokenizer, GPT2LMHeadModel,TextGenerationPipeline, GPT2Config
from lora_train import get_finedtuned_model_path
import wordninja
import re
import unicodedata

load_dotenv()
# Lazy-load cache
_qa_pipeline = {}

CUSTOM_TEMPLATE = """You are a helpful AI assistant. Use the context below to answer the user's question.

Context:
{context}

Question:
{question}

Answer:"""

prompt = PromptTemplate(
    input_variables=["context", "question"],
    template=CUSTOM_TEMPLATE
)


def get_qa_pipeline(filename: str, model_choice: str):
    global _qa_pipeline
    key = f"{filename}_{model_choice}"
    if key in _qa_pipeline:
        return _qa_pipeline[key]

    try:
        print("[DEBUG] Loading RAG pipeline")

        model_path = get_finedtuned_model_path(filename, model_choice)
        tokenizer_path = os.path.join(model_path, "_tokenizer")
        
        if not os.path.isdir(model_path):
            raise ValueError(f"Model path {model_path} is not a directory. Cannot load locally.")
        
        HF_CACHE = "/tmp/hf_cache"

        print("[DEBUG] Loading tokenizer...")
        
        model = GPT2LMHeadModel.from_pretrained( 
            pretrained_model_name_or_path=model_path,
            cache_dir=HF_CACHE,
            local_files_only=True,
            trust_remote_code=True,
            use_safetensors=True
        )
        model.to("cpu")

        tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, cache_dir=HF_CACHE, use_fast=False, local_files_only=True, add_prefix_space=True)

        llm_pipeline = TextGenerationPipeline(
            model=model,
            tokenizer=tokenizer,
            max_new_tokens=200,
            do_sample=True,
            temperature=0.7,
            top_p=0.95,
            clean_up_tokenization_spaces=True,
            return_full_text=False
        )

        llm = HuggingFacePipeline(pipeline=llm_pipeline,
                                  model_id=model_path)

        EMBED_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
        CHROMA_PATH = os.getenv("CHROMA_PATH", "./chroma_db")
        embedding_function = HuggingFaceEmbeddings(model_name=EMBED_MODEL_NAME)
        vectordb = Chroma(persist_directory=CHROMA_PATH, embedding_function=embedding_function)

        def extract_text(output):
            try:
                if isinstance(output, list) and len(output) > 0:
                    if isinstance(output[0], dict) and "generated_text" in output[0]:
                        return output[0]["generated_text"]
                    else:
                        return str(output[0])
                elif isinstance(output, dict):
                    return output.get("generated_text", str(output))
                elif isinstance(output, str):
                    return output
                return str(output)
            except Exception as e:
                print(f"[extract_text ERROR] {e}")
                return f"RAG post-processing error: {e}"

        chain = RunnableMap({
            "context": lambda x: x["context"],
            "question": lambda x: x["question"]
        }) | prompt | llm | RunnableLambda(lambda x: x[0] if isinstance(x, list) else x) | StrOutputParser()
        
        _qa_pipeline[key] = (chain, vectordb)

        print("✅ QA Pipeline loaded successfully.")
        return _qa_pipeline[key]

    except Exception as e:
        print(f"❌ Failed to load QA pipeline: {e}")
        import traceback
        traceback.print_exc()
        return None
    
def clean_response(text: str) -> str:
    # Normalize unicode
    text = unicodedata.normalize("NFKC", text)

    # Collapse repeated phrases
    text = re.sub(r'\b(\w{3,20})( \1\b)+', r'\1', text)

    # Add space between lowercase-uppercase or letter-digit
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[a-zA-Z])(?=[0-9])", " ", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)

    # Collapse multiple spaces and line breaks
    text = re.sub(r" +", " ", text)
    text = re.sub(r"\n{2,}", "\n\n", text)

    text = " ".join(wordninja.split(text))
    print(f"\n\n{text.strip()}")

    return text.strip()
    
def run_qa(query: str, filename: str, model_choice: str) -> str:
    """
    Create a RAG QA response to a user's question
    """
    pair = get_qa_pipeline(filename, model_choice)

    if pair is None:
        return "QA pipeline is not ready"
    
    chain, vectordb = pair
    relevant_docs = vectordb.similarity_search(query, k=4)
    context = "\n".join([doc.page_content for doc in relevant_docs])

    try:
        response = chain.invoke({"context": context, "question": query})
        print("[DEBUG] Raw response:", response)
        return clean_response(response)
    except Exception as e:
        print(f"Error during RAG invoke: {e}")
        return f"RAG error: {str(e)}"
