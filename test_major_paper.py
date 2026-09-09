import json,tempfile,unittest
from pathlib import Path
import major_paper as p

class T(unittest.TestCase):
    def seed(self,db,now=1000,direction=1):
        db.execute("CREATE TABLE bot_decisions(id INTEGER PRIMARY KEY,ts REAL,scope TEXT,action TEXT,symbol TEXT,metadata_json TEXT)")
        db.execute("CREATE TABLE major_quotes(symbol TEXT PRIMARY KEY,ts_ms INTEGER,bid REAL,ask REAL,source TEXT)")
        db.execute("CREATE TABLE major_ticks(symbol TEXT,trade_id INTEGER,ts_ms INTEGER,price REAL,qty REAL,buyer_maker INTEGER,source TEXT)")
        db.execute("CREATE TABLE major_leadlag_states(id INTEGER PRIMARY KEY,expected_move_bps REAL)")
        action='OPEN_LONG_MAJOR_PAPER' if direction>0 else 'OPEN_SHORT_MAJOR_PAPER'
        db.execute("insert into bot_decisions values(1,?,?,?,?,?)",(now,'MAJOR',action,'SOL',json.dumps({'lag_state_id':1,'best_lag_s':10})))
        db.execute("insert into major_leadlag_states values(1,50)")
        db.execute("insert into major_quotes values('SOL',?,?,?,'x')",(now*1000,99.9,100.1));db.commit()
    def test_long_opens(self):
        with tempfile.TemporaryDirectory() as td:
            db=p.dbopen(Path(td)/'x.db');self.seed(db);d=db.execute('select * from bot_decisions').fetchone();self.assertIsNotNone(p.open_decision(db,d,1000));db.close()
    def test_short_profit_when_price_falls(self):
        with tempfile.TemporaryDirectory() as td:
            db=p.dbopen(Path(td)/'x.db');self.seed(db,direction=-1);d=db.execute('select * from bot_decisions').fetchone();p.open_decision(db,d,1000)
            db.execute("update major_quotes set ts_ms=?,bid=?,ask=? where symbol='SOL'",(1001000,98.9,99.1));db.commit();pos=db.execute('select * from major_paper_positions').fetchone();r=p.manage(db,pos,1001)
            row=db.execute('select net_pnl from major_paper_positions').fetchone();self.assertIn('CLOSED',r);self.assertGreater(row[0],0);db.close()

    def test_flat_roundtrip_is_negative_only_from_execution_costs(self):
        with tempfile.TemporaryDirectory() as td:
            now=1000.0;db=p.dbopen(Path(td)/'x.db');self.seed(db,now=now,direction=1)
            d=db.execute("select * from bot_decisions").fetchone();pid=p.open_decision(db,d,now)
            pos=db.execute("select * from major_paper_positions where id=?",(pid,)).fetchone();q=p.quote(db,'SOL',now+.1)
            pnl=p.close(db,pos,q,now+.1,'FLAT')
            self.assertLess(pnl,0);self.assertGreater(pos['entry_fee'],0)
            self.assertGreater(db.execute("select exit_fee from major_paper_positions where id=?",(pid,)).fetchone()[0],0);db.close()

    def test_time_exit(self):
        with tempfile.TemporaryDirectory() as td:
            db=p.dbopen(Path(td)/'x.db');self.seed(db);d=db.execute('select * from bot_decisions').fetchone();p.open_decision(db,d,1000);db.execute("update major_quotes set ts_ms=?",(1100000,));db.commit();pos=db.execute('select * from major_paper_positions').fetchone();self.assertEqual(p.manage(db,pos,1100),'CLOSED_TIME');db.close()
    def test_short_scorecard_labels_synthetic(self):
        with tempfile.TemporaryDirectory() as td:
            db=p.dbopen(Path(td)/'x.db');self.assertIn('SYNTHETIC',p.scorecard(db)['short_execution_note']);db.close()
if __name__=='__main__':unittest.main()
