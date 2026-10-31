"""Export reviewed promotion engine baseline and menu/BOM scenario inputs, no raw CSVs."""
from pathlib import Path
from datetime import date,timedelta
import sys,json,argparse
p=argparse.ArgumentParser();p.add_argument('--package',type=Path,default=Path(__file__).resolve().parents[2]/'model');p.add_argument('--data',type=Path,required=True);a=p.parse_args()
sys.path.insert(0,str(a.package.resolve()))
from promotion_forecast.bootstrap import load_engine
load_engine(a.package)
from promotion_forecast import make_forecaster
from promotion_forecast.core import reference_response
from src.loader import load_all
from src import prep
from src.engine.params import EngineParams
from src.engine.requirement import ingredient_requirement
import pandas as pd
root=Path(__file__).resolve().parents[1];ds=load_all(a.data);daily=prep.daily_sales(ds)
menus=ds.get('02_menus')[['menu_id','menu_name','is_set','virtual_price']].to_dict('records')
bom=ds.get('04_bom')[['menu_id','ingredient_id','qty_per_menu','use_unit']].to_dict('records')
windows={};scenario={};asofs=['2026-09-09','2026-09-16','2026-09-23']+[(date(2026,9,30)+timedelta(days=i)).isoformat() for i in range(15)]
for asof in asofs:
 origin=min(pd.Timestamp(asof),pd.Timestamp('2026-09-30'));params=EngineParams(asof_date=origin,horizon_days=(pd.Timestamp(asof)-origin).days+7,apply_trend=False)
 fc,_=make_forecaster({'schema_version':1,'promotions':[]})(ds,params,daily=daily)
 fc=fc[fc.date>pd.Timestamp(asof)];req=ingredient_requirement(fc,ds)
 stores={};menu_stores={}
 for sid in ['S01','S02','S03']:
  stores[sid]={iid:[float(v) for v in g.sort_values('date').requirement_qty] for iid,g in req[req.store_id==sid].groupby('ingredient_id')}
  menu_stores[sid]={mid:{'qty':[float(v) for v in g.sort_values('date').baseline_forecast_qty],'available':[int(v) for v in g.sort_values('date').is_available]} for mid,g in fc[fc.store_id==sid].groupby('menu_id')}
 refs=[]
 for row in ds.get('08_promotions').itertuples():
  if row.end_date>origin:continue
  plan={'target_menu_ids':list(row.target_menu),'response':{'reference_promo_id':row.promo_id}}
  ref={'id':row.promo_id,'store':row.store_id,'menus':list(row.target_menu),'period':[str(row.start_date.date()),str(row.end_date.date())]}
  try:ref['factors']={mid:{'factor':v[0],'note':v[1]} for mid,v in reference_response(ds,daily,plan,params).items()}
  except ValueError as e:ref['error']=str(e)
  refs.append(ref)
 dates=[str(v.date()) for v in sorted(fc.date.unique())]
 windows[asof]={'data_through':str(origin.date()),'dates':dates,'stores':stores}
 scenario[asof]={'stores':menu_stores,'references':refs}
team={'model_id':'team-promotion-forecast-v1','model_name':'팀 프로모션 수정 모델 · 비행사 기준 수요 + 명시적 행사 반응','sales_through':'2026-09-30','coverage_end':'2026-10-14','windows':windows}
(root/'dist/team-forecasts.json').write_text(json.dumps(team,ensure_ascii=False,allow_nan=False,separators=(',',':')))
(root/'dist/promotion-inputs.json').write_text(json.dumps({'menus':menus,'bom':bom,'windows':scenario},ensure_ascii=False,allow_nan=False,separators=(',',':')))
# Python integration fixture: S02 M004/M005 +30%, selected dates only.
params=EngineParams(asof_date=pd.Timestamp('2026-09-30'),horizon_days=7,apply_trend=False)
plan=json.loads((a.package/'promotion_plan.example.json').read_text())
fc,_=make_forecaster(plan)(ds,params,daily=daily);req=ingredient_requirement(fc,ds)
fixture={'plan':plan,'expected':{sid:{iid:[float(v) for v in g.sort_values('date').requirement_qty] for iid,g in req[req.store_id==sid].groupby('ingredient_id')} for sid in ['S01','S02','S03']}}
(root/'scripts/promotion-fixture.json').write_text(json.dumps(fixture,ensure_ascii=False))
print('Exported updated baseline, menu/BOM inputs and Python reference fixture:',len(windows),'windows')
