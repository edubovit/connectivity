import argparse
import asyncio
import json
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def check(browser_type, args):
    browser=await browser_type.launch()
    context=await browser.new_context(viewport={"width":1280,"height":1000})
    page=await context.new_page()
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    url=args.url.rstrip('/')+'/'
    try:
        await page.goto(url+'#speedtest')
        await page.locator('#password').fill(os.environ.get('CONNECTIVITY_TEST_PASSWORD','test-only-password'))
        await page.locator('#login-form button').click()
        await page.locator('#speedtest').wait_for(state='visible')
        before=await (await page.request.get(url+'api/v1/status')).json()
        async with page.expect_response(lambda response:response.url==url+'api/v1/speedtest/runs' and response.request.method=='POST') as pending:
            await page.locator('#speed-start').click()
        started=await (await pending.value).json()
        await expect(page.locator('#speed-result-heading')).to_have_text(re.compile(r'^Latest attempt:'),timeout=65000)
        heading=await page.locator('#speed-result-heading').text_content()
        if 'completed' not in heading:
            raise AssertionError(await page.locator('#speed-result-error').text_content())
        result_response=await page.request.get(url+'api/v1/speedtest/runs/'+started['id'])
        assert result_response.status==200
        result=await result_response.json()
        assert all(result['metrics'][key]['complete'] and result['metrics'][key]['measurement_seconds']==10 for key in ['download','upload'])
        after=await (await page.request.get(url+'api/v1/status')).json()
        assert [r['reset_at'] for r in before['resources']]==[r['reset_at'] for r in after['resources']]
        for old,new in zip(before['resources'],after['resources']):
            assert [c['generation'] for c in old['checks']]==[c['generation'] for c in new['checks']]
        await expect(page.locator('#speed-start')).to_be_enabled(timeout=5000)
        await page.locator('#speed-start').click()
        await expect(page.locator('#speed-cancel')).to_be_visible(timeout=5000)
        await page.locator('#speed-cancel').click()
        await expect(page.locator('#speed-result-heading')).to_have_text(re.compile(r'^Latest attempt: cancelled'),timeout=8000)
        await page.reload()
        await page.locator('#speedtest').wait_for(state='visible')
        assert await page.locator('#speed-result-heading').text_content()=='No result yet'
        await page.locator('#logout').click()
        await page.locator('#login').wait_for(state='visible')
        assert not errors,errors
        return {"engine":browser_type.name,"version":browser.version,"outcome":result['outcome'],"metrics":result['metrics'],
                "monitoring_generations_preserved":True,"cancel_passed":True,"reload_clears_browser_result":True,"page_errors":errors}
    finally:
        await context.close();await browser.close()


async def main(args):
    report=[]
    async with async_playwright() as playwright:
        for name in args.browsers:
            report.append(await check(getattr(playwright,name),args))
    Path(args.report).write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Explicitly runs full-duration tests against a controlled compatible fixture; may transfer gigabytes. Browser tooling is a development-only dependency.')
    parser.add_argument('--url',default='http://127.0.0.1:18092/')
    parser.add_argument('--browsers',nargs='+',choices=['chromium','firefox','webkit'],default=['firefox','webkit'])
    parser.add_argument('--report',default='.local/speed-browser-engines.json')
    args=parser.parse_args()
    try:
        asyncio.run(main(args))
    except Exception as error:
        message=str(error).replace(os.environ.get('CONNECTIVITY_TEST_PASSWORD','test-only-password'),'<redacted>')
        message=re.sub(r'connectivity_session=[^\s;]+','connectivity_session=<redacted>',message)
        print(f'Browser verification failed ({type(error).__name__}): {message}')
        raise SystemExit(1)
