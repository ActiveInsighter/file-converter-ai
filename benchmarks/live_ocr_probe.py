import asyncio,json,os
from pathlib import Path
import httpx
import paddle_ocr as po

async def main():
 root=Path('output'); root.mkdir(exist_ok=True)
 async with httpx.AsyncClient(follow_redirects=True) as client:
  for mode in ('baseline','large'):
   options={**po.OPTIONAL_PAYLOAD}
   if mode=='large': options['layoutMergeBboxesMode']={'14':'large'}
   for number in (190,284):
    ocr=po.PaddleOcr(po.parse_tokens(os.environ['PADDLEOCR_TOKENS']),client=client,work_dir=root/mode,source_url=os.environ['SOURCE_URL'])
    response=await client.post(po.JOB_URL,headers=ocr._next_token_headers(),json={'fileUrl':ocr.remote_url,'model':po.DEFAULT_MODEL,'pageRanges':f'{number}-{number}','optionalPayload':options},timeout=300)
    data=ocr._check(response)['jobId']
    job=(data,{'Authorization':'bearer '+ocr.tokens[0]})
    results=await ocr._collect(job,f'{mode}-{number}',1,600)
    (root/f'{mode}-{number}.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    page=await ocr._page_from_result(number,3,results[0])
    (root/f'{mode}-{number}.md').write_text(page.markdown)
    print(mode,number,len(page.figures),flush=True)
asyncio.run(main())
