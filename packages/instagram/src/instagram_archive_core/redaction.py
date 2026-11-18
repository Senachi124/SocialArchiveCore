"""Suppress transient credentials before any child-process output is persisted."""
import re


def redact(value):
    value=str(value)
    value=re.sub(r'https?://[^\s\]\)<>]+','[url]',value)
    value=re.sub(r'(?i)\b(cookie|authorization|sessionid|csrftoken|access_token|password)\s*[:=]\s*[^\r\n]+',r'\1=[redacted]',value)
    return value
