"""Read the supplied archive locally; publish only prepared dashboard views, never CSVs."""
from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta
import argparse, csv, io, json, zipfile

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data-zip', type=Path, required=True, help='제공받은 원본 CSV ZIP 경로 (저장소에 추가하지 마세요)')
archive = parser.parse_args().data_zip
with zipfile.ZipFile(archive) as z:
    def read(prefix):
        name = next(n for n in z.namelist() if n.split('/')[-1].startswith(prefix) and n.endswith('.csv'))
        return list(csv.DictReader(io.StringIO(z.read(name).decode('utf-8-sig'))))
    tables = {i: read(f'{i:02}_') for i in range(1,18)}

ingredients = {r['ingredient_id']: r for r in tables[3]}
bom = defaultdict(list)
for r in tables[4]: bom[r['menu_id']].append((r['ingredient_id'],float(r['qty_per_menu'])))
sales = defaultdict(float)
for r in tables[9]:
    # Potential non-cancelled demand, including lost sales; separately labelled baseline.
    sales[r['store_id'],r['date'],r['menu_id']] += max(0,float(r['demand_qty'])-float(r['cancel_qty']))
inventory = {(r['date'],r['store_id'],r['ingredient_id']):r for r in tables[10]}
lots = defaultdict(list)
for r in tables[16]:
    lots[r['date'],r['store_id'],r['ingredient_id']].append({'qty':float(r['remaining_qty']),'expiry':r['last_use_date'],'received':r['receipt_date']})
receipts=defaultdict(list)
for r in tables[12]: receipts[r['po_line_id']].append(r)
receipts_daily=defaultdict(list)
for r in tables[12]:
    receipts_daily[r['receipt_date'],r['store_id'],r['ingredient_id']].append({
        'po':r['po_id'],'line':r['po_line_id'],'date':r['receipt_date'],
        'packs':float(r['received_qty']),'unit':r['order_unit'],
        'qty':float(r['received_use_qty']),'status':r['status']})
waste_daily=defaultdict(list)
for r in tables[13]:
    waste_daily[r['date'],r['store_id'],r['ingredient_id']].append({
        'qty':float(r['waste_qty']),'reason':r['reason'],'cost':float(r['waste_cost'])})
out={'stores':tables[1],'suppliers':tables[5],'dates':['2026-09-09','2026-09-16','2026-09-23','2026-09-30'],'snapshots':{}}
for ds in out['dates']:
    ref=date.fromisoformat(ds)
    for st in tables[1]:
        sid=st['store_id']; forecast=defaultdict(lambda:[0.0]*7)
        for offset in range(1,8):
            future=ref+timedelta(days=offset)
            for m in tables[2]:
                mid=m['menu_id']
                eligible=any(r['store_id']==sid and r['menu_id']==mid and r['status']=='취급' and r['start_date']<=ds<=r['end_date'] for r in tables[15])
                if not eligible: continue
                history=[]
                for j in range(28):
                    d=ref-timedelta(days=j)
                    if d.weekday()==future.weekday() and any(r['store_id']==sid and r['menu_id']==mid and r['status']=='취급' and r['start_date']<=d.isoformat()<=r['end_date'] for r in tables[15]):
                        history.append(sales[sid,d.isoformat(),mid])
                avg=sum(history)/len(history) if history else 0
                for iid,q in bom[mid]: forecast[iid][offset-1]+=avg*q
        items=[]
        for iid,ing in ingredients.items():
            prices=[r for r in tables[6] if r['ingredient_id']==iid and r['valid_from']<=ds]
            price=float(max(prices,key=lambda r:r['valid_from'])['unit_price'])
            inv=inventory[ds,sid,iid]
            pending=[]
            for p in tables[11]:
                if p['store_id']!=sid or p['ingredient_id']!=iid or p['order_date']>ds: continue
                arrived=sum(float(r['received_qty']) for r in receipts[p['po_line_id']] if r['receipt_date']<=ds)
                remain=max(0,float(p['order_qty'])-arrived)
                if remain: pending.append({'date':p['expected_date'],'qty':remain*float(ing['use_qty_per_order_unit'])})
            history=[]
            for j in range(13,-1,-1):
                d=(ref-timedelta(days=j)).isoformat()
                history.append({'date':d,'qty':float(inventory[d,sid,iid]['closing_qty'])})
            item={**ing,'price':price,'stock':float(inv['closing_qty']),'lots':lots[ds,sid,iid], 'forecast':[round(x,2) for x in forecast[iid]],'pending':pending,'history':history}
            item['actual']={k:float(inv[k]) for k in [
                'opening_qty','received_qty','adjust_qty','counted_qty','consumed_qty',
                'waste_qty','transfer_in_qty','transfer_out_qty','closing_qty','closing_value']}
            item['actual']['receipts']=receipts_daily[ds,sid,iid]
            item['actual']['waste_records']=waste_daily[ds,sid,iid]
            a=item['actual']
            assert abs(sum(l['qty'] for l in item['lots'])-item['stock'])<0.01,(ds,sid,iid,'lot mismatch')
            assert abs(sum(r['qty'] for r in a['receipts'])-a['received_qty'])<0.01,(ds,sid,iid,'receipt mismatch')
            assert abs(sum(r['qty'] for r in a['waste_records'])-a['waste_qty'])<0.01,(ds,sid,iid,'waste mismatch')
            expected=a['opening_qty']+a['received_qty']+a['adjust_qty']+a['transfer_in_qty']-a['transfer_out_qty']-a['consumed_qty']-a['waste_qty']
            assert abs(expected-item['stock'])<0.01,(ds,sid,iid,'balance mismatch')
            for key in ['use_qty_per_order_unit','shelf_life_days','min_order_qty']: item[key]=float(item[key])
            items.append(item)
        out['snapshots'][sid+'_'+ds]=items
(ROOT/'dist'/'data.json').write_text(json.dumps(out,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
purchase_history = []
for p in tables[11]:
    ing = ingredients[p['ingredient_id']]
    purchase_history.append({
        'id': p['po_line_id'], 'po': p['po_id'], 'store': p['store_id'],
        'date': p['order_date'], 'time': p['order_time'], 'supplier': p['supplier_id'],
        'ingredient': p['ingredient_id'], 'name': ing['ingredient_name'],
        'quantity': float(p['order_qty']), 'unit': p['order_unit'],
        'price': float(p['unit_price']), 'amount': float(p['order_amount']),
        'expected': p['expected_date'], 'type': p['order_type'],
        'receipts': [{'date':r['receipt_date'], 'qty':float(r['received_qty']),
                      'status':r['status']} for r in receipts[p['po_line_id']]]
    })
(ROOT/'dist'/'purchase-history.json').write_text(json.dumps({
    'period': {'start':'2026-01-01','end':'2026-09-30'},
    'ingredients': [{'id':key,'name':value['ingredient_name']} for key,value in ingredients.items()],
    'lines': purchase_history
},ensure_ascii=False,separators=(',',':')),encoding='utf-8')
print('Prepared',len(out['snapshots']),'store-date views; no raw CSV copied.')
