"""Phase 3 browser QA uses an isolated database and simulated destination only."""
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen
import uvicorn
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import ROOT, Settings
from app.main import create_app

artifacts=ROOT/'artifacts'
artifacts.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='blog-phase3-') as temporary:
    app=create_app(Settings(Path(temporary)/'qa.db',demo=True))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    base=f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                with urlopen(base+'/health',timeout=1) as response: assert json.load(response)['phase']==3
                break
            except OSError: time.sleep(.1)
        else: raise RuntimeError('Server did not start')
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
            page.goto(base+'/destination',wait_until='networkidle')
            page.get_by_role('button',name='Check connection',exact=True).click()
            expect(page.locator('#destination-message')).to_contain_text('Simulation ready')
            page.screenshot(path=str(artifacts/'phase3-destination.png'),full_page=True)
            page.goto(base+'/posts/1/import',wait_until='networkidle')
            expect(page.locator('main')).to_contain_text('Review and approve this post before importing')
            expect(page.locator('#draft-import')).to_have_count(0)
            page.goto(base+'/posts/1',wait_until='networkidle')
            page.locator('#acknowledge-warnings').check()
            page.get_by_role('button',name='Approve for import',exact=False).click()
            expect(page.get_by_role('link',name='Import as draft',exact=False)).to_be_visible()
            page.wait_for_load_state('networkidle')
            page.get_by_role('link',name='Import as draft',exact=False).click()
            expect(page.get_by_label('Destination author',exact=True)).to_have_value('demo-sarah')
            page.get_by_label('Destination categories',exact=True).select_option(['demo-museum','demo-storage'])
            page.get_by_text('Review outgoing HTML',exact=True).click()
            expect(page.locator('.outgoing-html pre')).to_contain_text('Great spaces start')
            page.screenshot(path=str(artifacts/'phase3-import.png'),full_page=True)
            page.get_by_role('button',name='Create simulated draft',exact=True).click()
            expect(page.locator('.import-success')).to_contain_text('Simulated draft created',timeout=15000)
            expect(page.locator('#draft-import')).to_have_count(0)
            imported=app.state.db.get_post(1)
            assert imported['migration_status']=='IMPORTED'
            attempt=app.state.db.latest_import(1)
            assert attempt['payload']['status']=='DRAFT'
            assert attempt['payload']['categories']==['demo-museum','demo-storage']
            assert attempt['simulated']==1 and attempt['status']=='SUCCESS'
            page.screenshot(path=str(artifacts/'phase3-import-success.png'),full_page=True)
            page.get_by_role('link',name='Return to post preview',exact=True).click()
            expect(page.get_by_role('link',name='View import',exact=True)).to_be_visible()
            expect(page.get_by_role('link',name='Edit post',exact=False)).to_have_count(0)
            expect(page.get_by_role('button',name='Approve for import',exact=False)).to_be_disabled()
            expect(page.get_by_role('button',name='Reject',exact=True)).to_be_disabled()
            page.get_by_role('link',name='All posts',exact=False).click()
            page.locator('#review-filter').select_option('IMPORTED')
            page.get_by_role('button',name='Filter',exact=True).click()
            expect(page.locator('.posts-table tbody tr')).to_have_count(1)
            expect(page.locator('.posts-table .review-imported')).to_contain_text('Imported')
            page.screenshot(path=str(artifacts/'phase3-imported-posts.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.goto(base+'/posts/1/import',wait_until='networkidle')
            assert page.locator('html').evaluate('element=>element.scrollWidth<=innerWidth')
            page.screenshot(path=str(artifacts/'phase3-import-mobile.png'),full_page=True)
            page.goto(base+'/destination',wait_until='networkidle')
            assert page.locator('html').evaluate('element=>element.scrollWidth<=innerWidth')
            page.screenshot(path=str(artifacts/'phase3-destination-mobile.png'),full_page=True)
            assert not errors,errors
            browser.close()
        print('Phase 3 browser checks passed: simulated connection, approval gate, author/category mapping, draft creation, locked imported post, imported filter, and mobile layouts.')
    finally:
        server.should_exit=True
        thread.join(timeout=15)
