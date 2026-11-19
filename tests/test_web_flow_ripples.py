"""Delayed same-node replay signals must survive until their drawing time."""
import unittest
from tests.test_web_e2e import PageInChrome


class FlowRipples(PageInChrome):
    def test_future_same_entity_ripples_wait_then_draw_and_expire(self):
        self.big_flow_hub(active=8, idle=60, executors=3)
        self.flow_open({'eval': "window.__flowRec.mode('h24');1"}, {'sleep': 800})
        got = self.page({'eval': '''(()=>{
          document.querySelector('.flow-speed .pause').click();
          const seek=document.querySelector('.flow-seek');seek.value=999;seek.dispatchEvent(new Event('input'));
          // The last eight signals include three folded executor signals at 600/720/840 ms.
          // Observe actual Canvas strokes, not a new production debug interface.
          const ctx=document.querySelector('.flow canvas').getContext('2d');
          const begin=ctx.beginPath,arc=ctx.arc,stroke=ctx.stroke;let current=null,seen=[];
          ctx.beginPath=function(...args){current=null;return begin.apply(this,args)};
          ctx.arc=function(x,y,r,...args){current={x,y,r};return arc.call(this,x,y,r,...args)};
          ctx.stroke=function(...args){
            const lead=window.__flowRec.at('lead');
            // a dashed circle is the ring of a lead with folded executors, not a ripple
            if(current&&Math.abs(current.x-lead.x)<0.01&&Math.abs(current.y-lead.y)<0.01&&this.lineWidth>0.5
               &&!this.getLineDash().length)
              seen.push(current.r);
            return stroke.apply(this,args);
          };
          try {return [0,600,120,120,900].map(ms=>{
            seen=[];try{window.__flowRec.step(ms);return {radii:seen,error:null}}
            catch(e){return {radii:seen,error:String(e)}};
          })} finally {ctx.beginPath=begin;ctx.arc=arc;ctx.stroke=stroke}
        })()'''})[-1]['eval']
        self.assertEqual([frame['error'] for frame in got], [None] * 5)
        self.assertEqual([len(frame['radii']) for frame in got], [0, 1, 2, 3, 0])
        for frame in got[1:4]:
            self.assertAlmostEqual(min(frame['radii']), 8)


def load_tests(loader, standard, pattern):
    return unittest.TestSuite([FlowRipples('test_future_same_entity_ripples_wait_then_draw_and_expire')])
