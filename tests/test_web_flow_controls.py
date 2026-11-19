"""Compact Flow selectors exercise the real bundled view with synthetic fleets."""
import tests  # noqa: F401
import os
import unittest
from tests.test_web_e2e import PageInChrome


class FlowControls(PageInChrome):
    def controls(self, large):
        self.big_flow_hub(active=8 if large else 3, idle=60 if large else 5, executors=3 if large else 2)
        self.flow_open()
        got=self.page({'eval':'[document.querySelectorAll(".flow-controls select").length,window.__flowRec.summary().big]'})[-1]['eval']
        self.assertEqual(got,[2,large])
        change='(selector,value)=>{const e=document.querySelector(selector);e.value=value;e.dispatchEvent(new Event("change",{bubbles:true}));}'
        self.page({'eval':f'window.pickFlow={change};pickFlow(".flow-period","h24");1'},
                  {'until':'!window.__flowRec.done()'},
                  {'eval':'pickFlow(".flow-rate","2");window.__flowRec.step(15000);1'})
        got=self.page({'eval':'[document.querySelector(".flow-period").value,document.querySelector(".flow-rate").value,Math.round(window.__flowRec.pos()*100)]'})[-1]['eval']
        self.assertEqual(got,['h24','2',50])
        self.page({'eval':'document.querySelector(".flow-speed .pause").focus();1'}, {'key':' '},
                  {'eval':'window.__flowRec.step(10000);1'})
        self.assertEqual(self.page({'eval':'Math.round(window.__flowRec.pos()*100)'})[-1]['eval'],50)
        # period picked with the mouse - focus stays in the list; space there is pause too, not an opened list
        self.page({'eval':'document.querySelector(".flow-period").focus();1'}, {'key':' '},
                  {'eval':'window.__flowRec.step(3000);1'})
        self.assertGreater(self.page({'eval':'Math.round(window.__flowRec.pos()*100)'})[-1]['eval'],50)
        self.page({'key':' '})
        self.assertTrue(self.page({'eval':'document.querySelector(".flow-speed .pause").classList.contains("is-paused")'})[-1]['eval'])
        self.page({'eval':'const seek=document.querySelector(".flow-seek");seek.value=900;seek.dispatchEvent(new Event("input"));window.__flowRec.step(16);1'})
        self.assertEqual(self.page({'eval':'Math.round(window.__flowRec.pos()*100)'})[-1]['eval'],90)
        self.page({'eval':'''(()=>{const c=document.querySelector('.flow canvas'),r=c.getBoundingClientRect(),n=window.__flowRec.at('act0');
          c.dispatchEvent(new PointerEvent('pointerdown',{pointerType:'touch'}));c.dispatchEvent(new MouseEvent('click',{clientX:r.left+n.x,clientY:r.top+n.y}));
          pickFlow('.flow-rate','4');pickFlow('.flow-rate','2');return 1;})()'''})
        got=self.page({'eval':'[document.querySelector(".flow-tip").hidden,document.querySelector(".flow-tip b").textContent]'})[-1]['eval']
        self.assertEqual(got,[False,'act0'])
        self.page({'eval':'''(()=>{const c=document.querySelector('.flow canvas'),r=c.getBoundingClientRect();c.dispatchEvent(new MouseEvent('click',{clientX:r.left+1,clientY:r.top+1}));
          const s=document.querySelector('.flow-seek');s.value=999;s.dispatchEvent(new Event('input'));window.__flowRec.step(2000);return 1;})()'''})
        # snapshot while paused - after the deferred seek rounds; the canvas resizes - the same 80 frames (one observer)
        self.page({'eval':'window.__flowCapture = () => { try { for(let i=0;i<80;i++)window.__flowRec.step(0); } catch(e) { window.__flowCaptureError=String(e.stack); } }; new ResizeObserver(window.__flowCapture).observe(document.querySelector(".flow canvas")); 1'})
        for width in (390,1280):
            got=self.page({'shot':os.path.join(self.state,'resize.png'),'width':width,'height':900},
                          {'until':'(()=>{const c=document.querySelector(".flow canvas");return Math.abs(c.width/devicePixelRatio-c.getBoundingClientRect().width)<1})()'},
                          {'eval':'window.__flowCapture(); 1'},
                          {'eval':'''(()=>{const group=document.querySelector('.flow-controls').getBoundingClientRect();
                            return {overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,
                            inside:group.left>=0&&group.right<=innerWidth,
                            controls:[...document.querySelectorAll('.flow-controls select,.flow-controls button')].map(e=>({label:e.getAttribute('aria-label'),height:e.getBoundingClientRect().height,small:getComputedStyle(e).fontSize})),
                            captureError:window.__flowCaptureError||null,owner:window.__flowRec.at('owner'),period:document.querySelector('.flow-period').value,speed:document.querySelector('.flow-rate').value};})()'''})[-1]['eval']
            self.assertIsNone(got['captureError'])
            self.assertLess(abs(got['owner']['x']-got['owner']['tx']),1)
            self.assertFalse(got['overflow']);self.assertTrue(got['inside']);self.assertEqual(got['period'],'h24');self.assertEqual(got['speed'],'2')
            self.assertEqual(len(got['controls']),3)
            for control in got['controls']:
                self.assertTrue(control['label']);self.assertGreaterEqual(control['height'],30);self.assertGreaterEqual(float(control['small'][:-2]),13)
        # slider released: the browser sends change after input (since #296 input holds the replay in place until change)
        self.page({'eval':'const s=document.querySelector(".flow-seek");s.value=900;s.dispatchEvent(new Event("input"));s.dispatchEvent(new Event("change"));1'})
        self.page({'eval':'document.querySelector(".flow-speed .pause").click();window.__flowRec.step(10000);1'})
        self.assertEqual(self.page({'eval':'[Math.round(window.__flowRec.pos()*100),document.querySelector(".flow-speed .pause").classList.contains("is-paused")]'})[-1]['eval'],[100,True])
        self.page({'eval':'document.querySelector(".flow-speed .pause").click();window.__flowRec.step(1000);1'})
        self.assertLess(self.page({'eval':'window.__flowRec.pos()'})[-1]['eval'],0.1)
        self.page({'eval':'pickFlow(".flow-period","live");1'},{'until':'window.__flowRec.done()'},
                  {'eval':'pickFlow(".flow-period","h1");1'},{'until':'!window.__flowRec.done()'})
        self.assertEqual(self.page({'eval':'document.querySelector(".flow-rate").value'})[-1]['eval'],'2')

    def test_selects_keep_arrow(self):
        # btn class on the list: .btn reset the shared select arrow (base.css) and the room for it - it looked like a button
        self.big_flow_hub(active=3, idle=5, executors=2)
        self.flow_open()
        got=self.page({'eval':'[...document.querySelectorAll(".flow-controls select")].map(e=>{const s=getComputedStyle(e);return [s.backgroundImage.includes("gradient"),parseFloat(s.paddingRight)>=20]})'})[-1]['eval']
        self.assertEqual(got,[[True,True],[True,True]])

    def test_small_fleet_compact_controls(self):
        self.controls(False)

    def test_large_fleet_compact_controls(self):
        self.controls(True)


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(FlowControls(n) for n in FlowControls.__dict__ if n.startswith('test_'))
