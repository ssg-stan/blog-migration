"""Phase 2 end-to-end checks, using an isolated demo database and mocked URL checks."""
import json
import sys
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen
import uvicorn
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import ROOT, Settings
from app.database import now
from app.main import create_app
from app.services.sync import normalize_post

ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)

with tempfile.TemporaryDirectory(prefix="blog-phase2-") as temporary:
    def mock_url_check(target):
        return {**target,"state":"broken","message":"Broken internal link: HTTP 404."}
    app=create_app(Settings(Path(temporary)/"qa.db",demo=True),url_checker=mock_url_check)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="error"))
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    base=f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urlopen(base+'/health',timeout=1) as response:
                    assert json.load(response)['phase']==3
                break
            except OSError:
                time.sleep(.1)
        else:
            raise RuntimeError("Test server did not start")
        # Add an internal-link base URL to the isolated source fixture.
        current=app.state.db.get_post(1)
        original=json.loads(current['raw_json'])
        original['url']='https://example.com/blog/article'
        def refresh(raw):
            normalized=normalize_post(raw,{str(raw['blogAuthorId']):'Sarah Williams'},
                                      {str(raw['tagIds'][0]):'Collections care'},now())
            run_id=app.state.db.start_sync()
            app.state.db.save_snapshot([normalized],run_id)
        refresh(original)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1100})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda message:errors.append(message.text) if message.type=='error' else None)
            page.goto(base+'/posts',wait_until='networkidle')
            expect(page.locator('.posts-table tbody tr')).to_have_count(6)
            page.screenshot(path=str(ARTIFACTS/'phase2-posts.png'),full_page=True)
            page.goto(base+'/posts/1',wait_until='networkidle')
            expect(page.get_by_role('heading',name='Content validation')).to_be_visible()
            page.screenshot(path=str(ARTIFACTS/'phase2-review.png'),full_page=True)
            page.get_by_role('link',name='Edit post',exact=False).click()
            expect(page.get_by_label('Title',exact=True)).to_have_value(original['name'])
            page.get_by_label('Title',exact=True).fill('Edited Museum Article')
            page.get_by_label('Categories',exact=True).fill('Museum')
            page.get_by_label('Body HTML',exact=True).fill('<div class="hs_cos_wrapper"><script>alert(1)</script><h2>Saved working content</h2><p>Local article.</p><a href="https://example.com/next">Next article</a></div>')
            page.get_by_role('button',name='Clean HTML',exact=False).click()
            expect(page.locator('#editor-message')).to_contain_text('HTML cleaned')
            cleaned=page.get_by_label('Body HTML',exact=True).input_value()
            assert '<script>' not in cleaned and 'hs_cos' not in cleaned
            page.screenshot(path=str(ARTIFACTS/'phase2-editor.png'),full_page=True)
            page.get_by_role('button',name='Save changes',exact=True).first.click()
            expect(page.get_by_role('heading',name='Content validation')).to_be_visible()
            frame=page.frame_locator('#article-preview')
            expect(frame.get_by_role('heading',name='Edited Museum Article',exact=True)).to_be_visible()
            page.get_by_role('tab',name='Original / Working',exact=False).click()
            expect(page.frame_locator('iframe[title="Original HubSpot article"]').get_by_role('heading',name=original['name'],exact=True)).to_be_visible()
            expect(page.frame_locator('iframe[title="Working local article"]').get_by_role('heading',name='Edited Museum Article',exact=True)).to_be_visible()
            page.screenshot(path=str(ARTIFACTS/'phase2-comparison.png'),full_page=True)
            page.get_by_role('button',name='Approve for import',exact=False).click()
            expect(page.locator('#review-message')).to_contain_text('check the acknowledgement')
            page.locator('#acknowledge-warnings').check()
            page.get_by_role('button',name='Approve for import',exact=False).click()
            expect(page.locator('.approval-card .review-approved')).to_be_visible()
            assert app.state.db.get_post(1)['migration_status']=='APPROVED'
            page.wait_for_load_state('networkidle')
            # Mock a broken URL; validation must clear approval and disable approval.
            page.get_by_role('button',name='Check URLs',exact=False).click()
            try:
                expect(page.locator('.validation-issues')).to_contain_text('HTTP 404',timeout=15000)
            except Exception:
                print('URL state', app.state.url_checks.status(1))
                checked=app.state.db.get_post(1)
                print('DB report', checked['link_report'], 'revision', checked['revision'])
                print('Browser state', page.locator('#url-check-status').inner_text(), 'revision', page.locator('.review-grid').get_attribute('data-revision'), 'errors', errors)
                page.screenshot(path=str(ARTIFACTS/'phase2-check-debug.png'), full_page=True)
                raise
            expect(page.locator('.approval-card .review-pending')).to_be_visible()
            expect(page.get_by_role('button',name='Approve for import',exact=False)).to_be_disabled()
            assert app.state.db.get_post(1)['migration_status']=='PENDING'
            # Refresh upstream source: working content survives and comparison warns about the change.
            upstream={**original,'name':'Upstream refreshed article','postBody':'<h2>Upstream content</h2><p>A refreshed source.</p>'}
            refresh(upstream)
            page.reload(wait_until='networkidle')
            expect(frame.get_by_role('heading',name='Edited Museum Article',exact=True)).to_be_visible()
            expect(page.locator('.validation-issues')).to_contain_text('HubSpot changed after local edits')
            page.get_by_role('link',name='Edit post',exact=False).click()
            page.get_by_label('Body HTML',exact=True).fill('<h2>Final article</h2><p>No broken links.</p>')
            page.get_by_role('button',name='Save changes',exact=True).first.click()
            expect(page.get_by_role('button',name='Approve for import',exact=False)).to_be_enabled()
            page.get_by_role('button',name='Reject',exact=True).click()
            expect(page.locator('.approval-card .review-rejected')).to_be_visible()
            page.get_by_role('link',name='All posts',exact=False).click()
            expect(page.locator('.posts-table tbody tr')).to_have_count(6)
            page.locator('#review-filter').select_option('REJECTED')
            page.get_by_role('button',name='Filter',exact=True).click()
            expect(page.locator('.posts-table tbody tr')).to_have_count(1)
            page.set_viewport_size({'width':390,'height':844})
            page.goto(base+'/posts/1',wait_until='networkidle')
            assert page.locator('html').evaluate('element=>element.scrollWidth<=innerWidth')
            page.screenshot(path=str(ARTIFACTS/'phase2-review-mobile.png'),full_page=True)
            page.goto(base+'/posts/1/edit',wait_until='networkidle')
            assert page.locator('html').evaluate('element=>element.scrollWidth<=innerWidth')
            page.screenshot(path=str(ARTIFACTS/'phase2-editor-mobile.png'),full_page=True)
            assert not errors,errors
            browser.close()
        print('Phase 2 browser checks passed: editing/cleaning, original comparison, approval/rejection, broken URL recovery, resync preservation, review filtering, and mobile layouts.')
    finally:
        server.should_exit=True
        thread.join(timeout=15)
