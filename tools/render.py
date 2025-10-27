from typing import List
from urllib.parse import urlparse
import markdownify as md

def enforce_lengths(title:str, subtitle:str, short:str):
    errs=[]
    if len(title)>60: errs.append("Title >60 chars")
    if len(subtitle)>110: errs.append("Subtitle >110 chars")
    if not (120<=len(short)<=160): errs.append("Short description not 120-160 chars")
    return errs

def build_article_html(title:str, subtitle:str, short:str, facts:List[str], why:List[str], source_url:str)->str:
    facts_md = "".join(f"- {f}\n" for f in facts)
    why_md   = "".join(f"- {w}\n" for w in why)
    host = urlparse(source_url).hostname or source_url
    md_text = f"""# {title}

_{subtitle}_

**Summary:** {short}

## Key Facts
{facts_md}

## Why it matters
{why_md}

### Source
[{host}]({source_url})
"""
    # Convert markdown to HTML (simple)
    html = md.markdownify(md_text, heading_style="ATX")
    return html
