import json,tempfile,time,unittest
from pathlib import Path
import external_events as e

RSS=b'''<rss><channel><item><guid>x1</guid><title>Federal Reserve cuts rate</title><link>https://x</link><pubDate>Tue, 08 Sep 2026 20:00:00 GMT</pubDate></item></channel></rss>'''
TG=b'''<div class="tgme_widget_message" data-post="GlobalFinance_ZH/123"><div class="tgme_widget_message_text js-message_text" dir="auto">Breaking: Strait of Hormuz attack reported</div><time datetime="2026-09-08T20:00:00+00:00"></time></div>'''

class T(unittest.TestCase):
    def test_rss_parse(self):
        r=e.parse_rss(RSS);self.assertEqual(r[0]['id'],'x1');self.assertIn('cuts rate',r[0]['title'])
    def test_telegram_parse(self):
        r=e.parse_telegram(TG);self.assertEqual(r[0]['id'],'GlobalFinance_ZH/123');self.assertIn('Hormuz',r[0]['title'])

    def test_free_official_source_coverage(self):
        names={x.name for x in e.SOURCES}
        for name in ("FED_ALL","FED_MONETARY","FED_SPEECHES","BLS_JOBS","BLS_CPI","BEA_RELEASES","WHITE_HOUSE","WHITE_HOUSE_ACTIONS","TREASURY","OFAC","HOUSE_FIN_SERVICES","SENATE_BANKING","CENTCOM","NHC_ATLANTIC","USGS_SIGNIFICANT","GLOBALFINANCE_ZH"):
            self.assertIn(name,names)

    def test_bea_macro_categories_are_structured_but_direction_ambiguous(self):
        self.assertEqual(e.classify_text("Personal Income and Outlays, August 2026")[:3],("INFLATION",.72,0))
        cat,sev,direction=e.classify_text("GDP (Third Estimate), 2nd Quarter 2026")
        self.assertEqual(cat,"GROWTH");self.assertGreaterEqual(sev,.66);self.assertEqual(direction,0)

    def test_war_high_severity_negative(self):
        cat,sev,d=e.classify_text('CENTCOM launches strikes after missile attack');self.assertEqual(cat,'WAR');self.assertGreaterEqual(sev,.9);self.assertEqual(d,-1)
    def test_cpi_direction_ambiguous(self):
        cat,sev,d=e.classify_text('Consumer Price Index August 2026');self.assertEqual(cat,'INFLATION');self.assertEqual(d,0)
    def test_bootstrap_html_is_inactive(self):
        src=e.Source('X','https://x','A','HTML','/news/')
        html=b'<a href="/news/old">Old important war headline</a>'
        with tempfile.TemporaryDirectory() as td:
            db=e.dbopen(Path(td)/'x.db');e.poll_source(db,src,1700000000,lambda u:html)
            self.assertEqual(db.execute('select active from external_events').fetchone()[0],0);db.close()
    def test_second_html_item_activates(self):
        src=e.Source('X','https://x','A','HTML','/news/')
        with tempfile.TemporaryDirectory() as td:
            db=e.dbopen(Path(td)/'x.db');e.poll_source(db,src,1700000000,lambda u:b'<a href="/news/a">Old item title</a>')
            e.poll_source(db,src,1700000030,lambda u:b'<a href="/news/b">New missile attack headline</a>')
            row=db.execute("select active from external_events where event_key like '%/b'").fetchone();self.assertEqual(row[0],1);db.close()
    def test_unconfirmed_telegram_low_reliability(self):
        x=e.ExternalEvent('D',1,1,0,False,-1,'WAR');self.assertLessEqual(e.event_reliability(x),.3)
if __name__=='__main__':unittest.main()
