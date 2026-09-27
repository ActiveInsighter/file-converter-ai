from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from pathlib import Path

import fitz
import gdown
import httpx

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
PAGES = [11, 20, 128, 156, 270]
PROMPT = """高精度数学 PDF 转录。下面 5 张图片依次对应 PDF 页 011、020、128、156、270。
逐页忠实转录为 Markdown，严禁总结或自行修正原文。
特别核对：指数、分式指数、积分上下限、区间端点、矩阵正负号、数字 0/2/4。
每页必须用精确标记开头：===== PAGE NNN =====
不要输出其它解释。"""


def keys() -> list[str]:
    return [x.strip() for x in re.split(r"[\r\n,;]+", os.environ["GEMINI_API_KEYS"]) if x.strip()]


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--output", required=True)
    args=p.parse_args()

    key=keys()[9]
    out=Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    pdf=out.parent/"source.pdf"
    gdown.download(url=args.source_url,output=str(pdf),quiet=True,fuzzy=True)
    doc=fitz.open(pdf)
    parts=[{"text":PROMPT}]
    for page in PAGES:
        pix=doc[page-1].get_pixmap(matrix=fitz.Matrix(240/72,240/72),alpha=False)
        data=pix.tobytes("jpeg",jpg_quality=95)
        parts.append({
            "inlineData":{
                "mimeType":"image/jpeg",
                "data":base64.b64encode(data).decode("ascii"),
            },
            "mediaResolution":{"level":"MEDIA_RESOLUTION_HIGH"},
        })
    doc.close()

    payload={
        "contents":[{"role":"user","parts":parts}],
        "generationConfig":{"thinkingConfig":{"thinkingLevel":"high"}},
    }

    client=httpx.Client(timeout=300.0)
    summary={"model":args.model,"pages":PAGES,"attempts":[]}
    for attempt in range(1,4):
        started=time.time()
        r=client.post(
            f"{API_BASE}/{args.model}:generateContent",
            headers={"x-goog-api-key":key,"Content-Type":"application/json"},
            json=payload,
        )
        elapsed=round(time.time()-started,2)
        summary["attempts"].append({"attempt":attempt,"status":r.status_code,"elapsed":elapsed})
        print(f"[challenge5] model={args.model} attempt={attempt} status={r.status_code} elapsed={elapsed}s",flush=True)
        if r.status_code==200:
            texts=[]
            for cand in r.json().get("candidates",[]):
                for part in (cand.get("content") or {}).get("parts",[]):
                    if part.get("thought"):
                        continue
                    if part.get("text"):
                        texts.append(part["text"])
            out.write_text("\n".join(texts).strip()+"\n",encoding="utf-8")
            summary["success"]=True
            (out.parent/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
            return 0
        summary["last_body"]=r.text[:1600]
        if r.status_code==503:
            time.sleep(30*attempt)
        elif r.status_code==429:
            time.sleep(15)
        else:
            break

    summary["success"]=False
    (out.parent/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    return 2


if __name__=="__main__":
    raise SystemExit(main())
