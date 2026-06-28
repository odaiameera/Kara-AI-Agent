import os
import glob
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "")

def _ensure_vault_path():
    if not OBSIDIAN_VAULT_PATH or not os.path.exists(OBSIDIAN_VAULT_PATH):
        raise ValueError(f"Obsidian vault path is not set or invalid: {OBSIDIAN_VAULT_PATH}. Please set OBSIDIAN_VAULT_PATH in .env")

def search_obsidian(query: str) -> str:
    """
    Search the Obsidian vault for markdown files containing the specific keyword query.
    
    Args:
        query: The keyword or phrase to search for across all notes.
        
    Returns:
        A formatted string listing the matching files and a small preview, or a message if nothing is found.
    """
    _ensure_vault_path()
    results = []
    
    # Very basic search implementation for MVP
    # Can be replaced with ChromaDB / vector search later
    vault_path = Path(OBSIDIAN_VAULT_PATH)
    for md_file in vault_path.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            if query.lower() in content.lower():
                # Extract a small snippet around the query
                idx = content.lower().find(query.lower())
                start = max(0, idx - 40)
                end = min(len(content), idx + 40)
                snippet = content[start:end].replace('\n', ' ')
                
                rel_path = md_file.relative_to(vault_path)
                results.append(f"- **{rel_path}**: \"...{snippet}...\"")
                
                if len(results) >= 10:  # Limit results to avoid massive context explosion
                    results.append("... (more results truncated)")
                    break
        except Exception:
            pass # Skip unreadable files
            
    if not results:
        return f"No notes found matching '{query}'."
    
    return "Search results:\\n" + "\\n".join(results)


def read_obsidian_note(file_path: str) -> str:
    """
    Read the full content of a specific note in the Obsidian vault.
    
    Args:
        file_path: The relative path to the markdown file inside the vault (e.g., '02 Parked/Parked Index.md').
        
    Returns:
        The content of the markdown file.
    """
    _ensure_vault_path()
    full_path = Path(OBSIDIAN_VAULT_PATH) / file_path
    
    if not full_path.exists():
        return f"Error: Note '{file_path}' does not exist."
        
    try:
        return full_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading note: {str(e)}"


def write_obsidian_note(file_path: str, content: str, overwrite: bool = False) -> str:
    """
    Create or append content to a note in the Obsidian vault.
    
    Args:
        file_path: The relative path to the markdown file (e.g., '04 Agents/New Memory.md').
        content: The markdown content to write to the file.
        overwrite: If True, overwrites the file. If False, appends to the end of the file.
        
    Returns:
        A success message indicating the file was saved.
    """
    _ensure_vault_path()
    full_path = Path(OBSIDIAN_VAULT_PATH) / file_path
    
    # Ensure parent directories exist
    full_path.parent.mkdir(parents=True, exist_ok=True)
    
    mode = 'w' if overwrite else 'a'
    try:
        with open(full_path, mode, encoding="utf-8") as f:
            if not overwrite and full_path.exists():
                f.write("\\n\\n") # Add spacing before appending
            f.write(content)
        action = "Overwritten" if overwrite else "Appended to"
        return f"Successfully {action.lower()} note at '{file_path}'."
    except Exception as e:
        return f"Error writing to note: {str(e)}"
