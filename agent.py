import os
import json
from google import genai
from google.genai import types
from dotenv import load_dotenv

from tools.obsidian_tools import search_obsidian, read_obsidian_note, write_obsidian_note

load_dotenv()

# We need a simple Core Memory structure
CORE_MEMORY = {
    "persona": "You are Kara, Odai's personal AI assistant. You have access to the user's Obsidian Vault for archival memory. You operate via a CLI interface. You manage your memory efficiently.",
    "human": "The user is Odai. They use Obsidian for taking notes, tracking projects, and parking ideas.",
    "active_task": "None"
}

def get_system_instruction() -> str:
    return f"""
{CORE_MEMORY['persona']}

HUMAN CONTEXT:
{CORE_MEMORY['human']}

CURRENT ACTIVE TASK:
{CORE_MEMORY['active_task']}

INSTRUCTIONS:
You have tools to access the user's Obsidian Vault. 
- If the user asks about their notes, projects, or parked ideas, use `search_obsidian` or `read_obsidian_note` to fetch the context before answering.
- If the user wants you to remember something, write it to the Obsidian vault using `write_obsidian_note` (e.g. creating a 'Kara Memory Bridge' note or a 'Daily Logs' note).
- Keep your answers concise and conversational in the CLI.
"""

def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Please set GEMINI_API_KEY in .env")
        return

    client = genai.Client(api_key=api_key)
    
    tools = [search_obsidian, read_obsidian_note, write_obsidian_note]
    
    # Initialize the chat session
    chat = client.chats.create(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            system_instruction=get_system_instruction(),
            tools=tools,
            temperature=0.0
        )
    )

    print("=========================================")
    print(" Personal AI Agent Initialized (MVP)")
    print(" Powered by Gemini + Obsidian Memory")
    print(" Type 'exit' to quit.")
    print("=========================================")
    
    while True:
        try:
            user_input = input("\\nYou: ")
            if user_input.lower() in ['exit', 'quit']:
                break
                
            response = chat.send_message(user_input)
            
            # Tool execution loop
            while response.function_calls:
                parts = []
                for function_call in response.function_calls:
                    func_name = function_call.name
                    args = function_call.args
                    print(f"\\n  [Agent is thinking... using tool: {func_name}({args})]")
                    
                    # Execute the tool
                    result = ""
                    try:
                        if func_name == "search_obsidian":
                            result = search_obsidian(**args)
                        elif func_name == "read_obsidian_note":
                            result = read_obsidian_note(**args)
                        elif func_name == "write_obsidian_note":
                            result = write_obsidian_note(**args)
                        else:
                            result = f"Error: Tool {func_name} not found."
                    except Exception as e:
                        result = f"Error executing {func_name}: {e}"
                        
                    parts.append(types.Part.from_function_response(
                        name=func_name,
                        response={"result": result}
                    ))
                
                # Send the tool responses back to the model
                response = chat.send_message(parts)
                
            if response.text:
                print(f"\\nAgent: {response.text}")
                
        except EOFError:
            break
        except Exception as e:
            print(f"\\n[!] Unexpected Error: {e}")

if __name__ == "__main__":
    main()
