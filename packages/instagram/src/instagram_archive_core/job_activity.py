"""Bounded activity pages and evidence-based historical media counts."""
import re
from pathlib import Path


def activity_page(db, limit=30, before=None, watch=()):
    limit=max(1,min(100,int(limit)))
    where='WHERE jobs.id<?' if before is not None else ''
    args=(int(before),limit+1) if before is not None else (limit+1,)
    query="SELECT jobs.*,accounts.label AS account_label FROM jobs LEFT JOIN accounts ON accounts.id=jobs.account_id "
    rows=db.execute(query+where+' ORDER BY jobs.id DESC LIMIT ?',args).fetchall()
    page=rows[:limit]
    cursor=page[-1]['id'] if len(rows)>limit else None
    watched=[]
    if watch:
        ids=tuple(dict.fromkeys(int(value) for value in watch))[:100]
        watched=db.execute(query+'WHERE jobs.id IN ('+','.join('?' for _ in ids)+') ORDER BY jobs.id DESC',ids).fetchall()
    return page,cursor,watched


def historical_added_count(row, library, runtime, extensions):
    # Old logs record a bare path only when a download succeeds. Skips have '#'
    # or an informational prefix. Do not require old files to remain on the VM.
    path=Path(row['log_path'] or '')
    if path.is_absolute() and not path.is_symlink() and path.resolve().is_relative_to(runtime.resolve()):
        try:
            saved=set()
            with path.open(encoding='utf-8',errors='replace') as stream:
                for line in stream:
                    line=re.sub(r'^\d{4}-\d\d-\d\dT\S+\s+', '', line.strip())
                    candidate=Path(line)
                    if candidate.is_absolute() and candidate.suffix.lower() in extensions:
                        resolved=candidate.resolve()
                        if resolved.is_relative_to(library.resolve()):saved.add(str(resolved))
            return len(saved)
        except (OSError,ValueError):pass
    match=re.search(r'(?:新增|已保存)\s*(\d+)\s*個媒體',row['message'] or '')
    return int(match[1]) if match else None
