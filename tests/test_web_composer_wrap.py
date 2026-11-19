"""Opt-in wrapping uses the real shared input without replacing its textarea or draft."""
import tests  # noqa: F401
import unittest
from tests.test_web_e2e import PageInChrome


class ComposerWrap(PageInChrome):
    def test_optional_wrapping_latches_until_empty_and_reflows_on_resize(self):
        scenario = r"""(async()=>{
          const {inputBox}=await import('/static/js/inputbox.js');
          const b=inputBox({key:'layout-probe',placeholder:'Message',hint:''}),f=b.node;
          f.classList.add('wrap-probe');document.body.append(f);
          const ta=f.querySelector('textarea'),original=ta;
          const style=document.createElement('style');style.textContent=`
            .wrap-probe {--composer-wrap-right:5px;--composer-wrap-gap:4px; width:360px;
              display:grid;grid-template-columns:minmax(0,1fr) 34px auto;grid-template-areas:'entry attach actions';
              padding:0 0 0 17px;gap:0;border:0;box-sizing:border-box;}
            .wrap-probe>.prompt {grid-area:entry;min-width:0;padding:6px 9px 6px 0;}
            .wrap-probe textarea {display:block;width:100%;min-width:0;box-sizing:border-box;border:0;
              font:13px/16px sans-serif;padding:3px 0;min-height:22px;}
            .wrap-probe>.attach {grid-area:attach;position:static;width:34px;height:34px;}
            .wrap-probe>.bar {grid-area:actions;min-width:34px;}
            .wrap-probe>.bar>.btn {width:34px;height:34px;margin:0;}
            .wrap-probe .g,.wrap-probe .hint,.wrap-probe .spacer {display:none;}
            .wrap-probe.composer-stacked {grid-template-areas:'entry entry entry' '. attach actions';}
            .wrap-probe.composer-stacked>.prompt {padding-right:5px;}
            * {transition-duration:.01ms!important;}`;document.head.append(style);
          const settle=()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))));
          const read=()=>({stacked:f.classList.contains('composer-stacked'),same:ta===f.querySelector('textarea'),
            mode:getComputedStyle(f).getPropertyValue('--composer-wrap'),width:f.getBoundingClientRect().width, value:ta.value,caret:[ta.selectionStart,ta.selectionEnd]});
          const set=async value=>{b.set({text:value});await settle();return read();};
          const out={default:await set('First\nSecond')};
          f.style.setProperty('--composer-wrap','1');out.enabled=await set('First\nSecond');
          ta.focus();ta.setSelectionRange(1,3);b.set({hint:''});await settle();out.caret=read();
          out.deleted=await set('Short');out.empty=await set('');
          out.long=await set('A long paragraph that wraps beside the controls. '.repeat(8));
          await set('');await set('Wrap when narrow');f.style.width='130px';await settle();out.narrow=read();
          f.style.width='360px';await settle();out.wide=read();out.reset=await set('');
          f.style.setProperty('--composer-wrap','0');out.disabled=await set('First\nSecond');
          const host=document.createElement('div');host.style.cssText='container-type:inline-size;width:300px';
          document.body.append(host);host.append(f);f.style.width='100%';f.style.removeProperty('--composer-wrap');
          style.textContent+=`.wrap-probe {--composer-wrap:0;} @container (min-width:500px) {.wrap-probe {--composer-wrap:1;}}`;
          out.phone=await set('Responsive\ntext');host.style.width='600px';await settle();out.desktop=read();
          host.style.width='300px';await settle();out.phoneAgain=read();
          // the latch must not outlive stacking: a break, then the width without the opt-in and a short draft, then back
          host.style.width='600px';await settle();await set('Line\nbreak');
          host.style.width='300px';await settle();await set('Short');
          host.style.width='600px';await settle();out.unlatched=read();
          window.__wrapResult=out;
        })().catch(e=>window.__wrapError=String(e.stack));"""
        got = self.page({'go':f'http://127.0.0.1:{self.port}/#/'},
                        {'until':'!!document.querySelector("main")'}, {'eval':scenario},
                        {'until':'!!window.__wrapResult || !!window.__wrapError'},
                        {'eval':'({result:window.__wrapResult,error:window.__wrapError})'})[-1]['eval']
        self.assertIsNone(got.get('error'))
        result = got['result']
        for name in ('enabled','deleted','long','narrow','wide','desktop'):
            self.assertTrue(result[name]['stacked'],(name,result))
        for name in ('default','empty','reset','disabled','phone','phoneAgain','unlatched'):
            self.assertFalse(result[name]['stacked'],name)
        self.assertEqual(result['caret']['caret'],[1,3])
        self.assertTrue(all(row['same'] for row in result.values()))
        self.assertEqual(result['disabled']['value'],'First\nSecond')


    def test_a_form_with_a_leading_picker_keeps_its_layout(self):
        scenario = r"""(async()=>{
          const {buildComposer}=await import('/static/js/composer.js');
          const pick=()=>{const b=document.createElement('button');b.className='btn';b.textContent='P';return b;};
          const c=buildComposer({key:'lead-probe',placeholder:'Message',hint:'',leading:pick,leadingSig:'1'}),f=c.node;
          f.style.setProperty('--composer-wrap','1');f.style.width='360px';document.body.append(f);
          c.setDraft({text:'First\nSecond'});
          await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
          window.__leadResult={leading:f.classList.contains('has-leading'),stacked:f.classList.contains('composer-stacked')};
        })().catch(e=>window.__leadError=String(e.stack));"""
        got = self.page({'go':f'http://127.0.0.1:{self.port}/#/'},
                        {'until':'!!document.querySelector("main")'}, {'eval':scenario},
                        {'until':'!!window.__leadResult || !!window.__leadError'},
                        {'eval':'({result:window.__leadResult,error:window.__leadError})'})[-1]['eval']
        self.assertIsNone(got.get('error'))
        self.assertEqual(got['result'], {'leading': True, 'stacked': False})


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(ComposerWrap(name) for name in ComposerWrap.__dict__ if name.startswith('test_'))
