"""A plugin's sidebar control shares the core's remembered layout."""
import tests  # noqa: F401
import unittest
from tests.test_web_e2e import PageInChrome


class SidebarAPI(PageInChrome):
    def test_public_sidebar_preserves_route_draft_and_remembered_width(self):
        self.views_plugin()
        self.assertEqual(self.page({'eval': 'typeof window.__viewsHub.sidebar'})[-1]['eval'], 'function')
        got = self.page({'eval': r'''(() => {
          const hub=window.__viewsHub,app=document.getElementById('app');
          const field=document.createElement('textarea');field.value='Unsent draft';
          document.querySelector('main').append(field);field.focus();
          const hash=location.hash,width=()=>Math.round(document.querySelector('aside').getBoundingClientRect().width);
          const events=[],off=hub.on('sidebar',state=>{events.push(state.open);state.open='mutated';});
          const before=[hub.capabilities.sidebar,hub.sidebar().open,width()];
          const closed=hub.sidebar({open:false});
          const afterClose=[closed.open,app.classList.contains('left-folded'),localStorage.getItem('hub.leftFolded')];
          const opened=hub.sidebar({open:true});hub.sidebar({open:true});off();
          hub.sidebar({open:false});hub.sidebar({open:true});
          return {before,afterClose,events,afterOpen:[opened.open,width(),location.hash===hash,
            field.isConnected,field.value,document.activeElement===field],
            invalid:hub.sidebar({open:'false'}).open};
        })()'''})[-1]['eval']
        self.assertEqual(got['before'], [1, True, 272])
        self.assertEqual(got['afterClose'], [False, True, '1'])
        self.assertEqual(got['afterOpen'], [True, 272, True, True, 'Unsent draft', True])
        self.assertTrue(got['invalid'])
        self.assertEqual(got['events'], [False, True])

    def test_css_sidebar_default_yields_to_an_explicit_resized_width(self):
        self.views_plugin()
        got = self.page({'eval': r'''(() => {
          const app=document.getElementById('app'),hub=window.__viewsHub;
          const width=()=>Math.round(document.querySelector('aside').getBoundingClientRect().width);
          app.style.setProperty('--sidebar-default-width','264px');hub.sidebar({open:false});hub.sidebar({open:true});const wide=width();
          app.style.setProperty('--sidebar-default-width','236px');const narrow=width();
          return [wide,narrow];
        })()'''})[-1]['eval']
        self.assertEqual(got, [264, 236])
        self.page({'eval': "localStorage.setItem('hub.leftW','360');location.reload();true"},
                  {'until':'!!window.__viewsHub'})
        got = self.page({'eval': r'''(() => {
          const app=document.getElementById('app'),hub=window.__viewsHub;
          app.style.setProperty('--sidebar-default-width','264px');
          hub.sidebar({open:false});hub.sidebar({open:true});
          return [Math.round(document.querySelector('aside').getBoundingClientRect().width),localStorage.getItem('hub.leftW')];
        })()'''})[-1]['eval']
        self.assertEqual(got,[360,'360'])

    def test_default_width_written_by_earlier_versions_is_not_an_explicit_width(self):
        self.views_plugin()
        self.page({'eval': "localStorage.setItem('hub.leftW','272');location.reload();true"},
                  {'until': '!!window.__viewsHub'})
        got = self.page({'eval': r'''(() => {
          document.getElementById('app').style.setProperty('--sidebar-default-width','236px');
          return Math.round(document.querySelector('aside').getBoundingClientRect().width);
        })()'''})[-1]['eval']
        self.assertEqual(got, 236)

    def test_phone_control_does_not_change_desktop_preference(self):
        self.views_plugin()
        got = self.page({'shot':f'{self.state}/sidebar-phone.png','width':390,'height':844},
                        {'eval':"window.__viewsHub.sidebar({open:false})"},
                        {'eval':"localStorage.getItem('hub.leftFolded')"})
        self.assertEqual(got[-2]['eval'], {'open':True,'available':False})
        self.assertIsNone(got[-1]['eval'])


def load_tests(loader, standard, pattern):
    return unittest.TestSuite(SidebarAPI(name) for name in SidebarAPI.__dict__ if name.startswith('test_'))
