import asyncio,json,os
from pathlib import Path
import httpx
import paddle_ocr as po

async def main():
 root=Path('output'); root.mkdir(exist_ok=True)
 async with httpx.AsyncClient(follow_redirects=True) as client:
  response=await client.get(os.environ['SOURCE_URL'],timeout=300)
  response.raise_for_status()
  source=root/'source.pdf'; source.write_bytes(response.content)
  for mode in ('baseline','large'):
   options={**po.OPTIONAL_PAYLOAD}
   if mode=='large': options['layoutMergeBboxesMode']={'14':'large'}
   for number in (190,284):
    ocr=po.PaddleOcr(po.parse_tokens(os.environ['PADDLEOCR_TOKENS']),client=client,work_dir=root/mode,source_url=os.environ['SOURCE_URL'])
    po.OPTIONAL_PAYLOAD.clear(); po.OPTIONAL_PAYLOAD.update(options)
    job=await ocr._submit_upload(po.extract_segment(source,number,number),f'{mode}-{number}')
    results=await ocr._collect(job,f'{mode}-{number}',1,600)
    (root/f'{mode}-{number}.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    page=await ocr._page_from_result(number,3,results[0])
    (root/f'{mode}-{number}.md').write_text(page.markdown)
    print(mode,number,len(page.figures),flush=True)
asyncio.run(main())
