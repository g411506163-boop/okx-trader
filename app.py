"""
OKX 模拟盘网格交易面板 - v5 PostgreSQL持久化版
"""

import hmac, hashlib, base64, time, json, threading, os
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

API_KEY    = os.environ.get("OKX_API_KEY", "")
SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

BASE_URL   = "https://www.okx.com"
SYMBOL     = "BTC-USDT"
GRID_LOW   = 66000
GRID_HIGH  = 73000
GRID_COUNT = 10

app = Flask(__name__)
CORS(app)

state = {
    "balance": 0, "initial_balance": 0, "pnl": 0.0,
    "trades": [], "price": 0, "wins": 0, "losses": 0,
    "total_trades": 0, "max_balance": 0, "max_drawdown": 0.0,
    "pnl_history": [], "grids": [], "log": [],
    "running": True, "api_connected": False, "start_time": ""
}

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS pnl_history (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP NOT NULL,
                balance NUMERIC NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY,
                ts TEXT, side TEXT, price INTEGER,
                qty NUMERIC, pnl NUMERIC
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        state["log"].insert(0, f"[{cn_time()}] 数据库连接成功！")
        return True
    except Exception as e:
        state["log"].insert(0, f"[{cn_time()}] 数据库错误: {e}")
        return False

def db_get(key):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key=%s", (key,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return row[0] if row else None
    except: return None

def db_set(key, value):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO meta(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, str(value)))
        conn.commit(); cur.close(); conn.close()
    except: pass

def db_save_pnl(balance):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO pnl_history(ts, balance) VALUES(%s,%s)",
                    (datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"), balance))
        conn.commit(); cur.close(); conn.close()
    except: pass

def db_save_trade(trade):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO trades(ts,side,price,qty,pnl) VALUES(%s,%s,%s,%s,%s)",
                    (trade["time"], trade["side"], trade["price"], trade["qty"], trade["pnl"]))
        conn.commit(); cur.close(); conn.close()
    except: pass

def db_load_pnl_history():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT to_char(ts,'YYYY-MM-DD HH24:MI'), CAST(balance AS FLOAT) FROM pnl_history ORDER BY id DESC LIMIT 2000")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"t": r[0], "v": r[1]} for r in reversed(rows)]
    except: return []

def db_load_trades():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT ts,side,price,CAST(qty AS FLOAT),CAST(pnl AS FLOAT) FROM trades ORDER BY id DESC LIMIT 50")
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"time":r[0],"side":r[1],"price":r[2],"qty":r[3],"pnl":r[4]} for r in rows]
    except: return []

def cn_now():
    return datetime.now(timezone(timedelta(hours=8)))

def cn_time():
    return cn_now().strftime("%H:%M:%S")

def sign(timestamp, method, path, body=""):
    msg = timestamp + method + path + (body or "")
    mac = hmac.new(SECRET_KEY.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def get_headers(method, path, body=""):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "x-simulated-trading": "1"
    }

def okx_get(path):
    try:
        r = requests.get(BASE_URL + path, headers=get_headers("GET", path), timeout=8)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def okx_post(path, body):
    b = json.dumps(body)
    try:
        r = requests.post(BASE_URL + path, headers=get_headers("POST", path, b), data=b, timeout=8)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def get_price():
    data = okx_get(f"/api/v5/market/ticker?instId={SYMBOL}")
    try:
        price = float(data["data"][0]["last"])
        state["api_connected"] = True
        return price
    except:
        state["api_connected"] = False
        return state["price"] or 69000

def get_balance():
    # 读取账户总资产（USDT计价，含BTC等所有持仓折算）
    data = okx_get("/api/v5/account/balance")
    try:
        return float(data["data"][0]["totalEq"])
    except:
        return state["balance"]

def build_grids():
    step = (GRID_HIGH - GRID_LOW) / GRID_COUNT
    return [{"price": round(GRID_LOW + i * step), "filled": False} for i in range(GRID_COUNT + 1)]

def run_grid(price):
    per_grid = state["initial_balance"] / GRID_COUNT if state["initial_balance"] > 0 else 500
    for g in state["grids"]:
        if not g["filled"] and price <= g["price"]:
            qty = round(per_grid / price, 6)
            result = okx_post("/api/v5/trade/order", {
                "instId": SYMBOL, "tdMode": "cash",
                "side": "buy", "ordType": "market", "sz": str(qty)
            })
            g["filled"] = True
            add_trade("买入", price, qty, result, 0)
        elif g["filled"] and price >= g["price"] * 1.008:
            qty = round(per_grid / price, 6)
            result = okx_post("/api/v5/trade/order", {
                "instId": SYMBOL, "tdMode": "cash",
                "side": "sell", "ordType": "market", "sz": str(qty)
            })
            g["filled"] = False
            pnl_est = round(g["price"] * 0.008 * qty, 4)
            add_trade("卖出", price, qty, result, pnl_est)

def add_trade(side, price, qty, api_result, pnl_val):
    now = cn_time()
    trade = {"time": now, "side": side, "price": round(price), "qty": qty, "pnl": round(pnl_val, 2)}
    state["trades"].insert(0, trade)
    if len(state["trades"]) > 50: state["trades"].pop()
    state["total_trades"] += 1
    if side == "卖出":
        if pnl_val > 0: state["wins"] += 1
        else: state["losses"] += 1
        db_set("wins", state["wins"])
        db_set("losses", state["losses"])
        db_set("total_trades", state["total_trades"])
    ok = "成功" if "data" in api_result else "失败"
    state["log"].insert(0, f"[{now}] {side} {qty} BTC @ ${round(price):,} → {ok}")
    if len(state["log"]) > 50: state["log"].pop()
    db_save_trade(trade)

def trading_loop():
    state["grids"] = build_grids()
    time.sleep(3)
    has_db = init_db()

    # 从数据库恢复状态
    if has_db:
        init_bal = db_get("initial_balance")
        if init_bal:
            state["initial_balance"] = float(init_bal)
            state["max_balance"] = float(db_get("max_balance") or init_bal)
            state["wins"] = int(db_get("wins") or 0)
            state["losses"] = int(db_get("losses") or 0)
            state["total_trades"] = int(db_get("total_trades") or 0)
            state["start_time"] = db_get("start_time") or ""
            state["pnl_history"] = db_load_pnl_history()
            state["trades"] = db_load_trades()
            state["log"].insert(0, f"[{cn_time()}] 历史数据已恢复，初始余额 ${state['initial_balance']:.2f}")

    first_fetch = (state["initial_balance"] == 0)

    while True:
        if state["running"]:
            try:
                price = get_price()
                state["price"] = round(price)
                bal = get_balance()
                state["balance"] = round(bal, 2)

                if first_fetch and bal > 0:
                    state["initial_balance"] = round(bal, 2)
                    state["max_balance"] = round(bal, 2)
                    state["start_time"] = cn_now().strftime("%Y-%m-%d %H:%M")
                    first_fetch = False
                    if has_db:
                        db_set("initial_balance", state["initial_balance"])
                        db_set("max_balance", state["max_balance"])
                        db_set("start_time", state["start_time"])
                    state["log"].insert(0, f"[{cn_time()}] 初始余额已记录：${bal:.2f}")

                if state["initial_balance"] > 0:
                    state["pnl"] = round(bal - state["initial_balance"], 2)
                    if bal > state["max_balance"]:
                        state["max_balance"] = bal
                        if has_db: db_set("max_balance", bal)
                    dd = (state["max_balance"] - bal) / state["max_balance"] * 100
                    state["max_drawdown"] = round(max(dd, 0), 2)

                point = {"t": cn_now().strftime("%Y-%m-%d %H:%M"), "v": round(bal, 2)}
                state["pnl_history"].append(point)
                if len(state["pnl_history"]) > 2000: state["pnl_history"].pop(0)
                if has_db: db_save_pnl(bal)

                if API_KEY: run_grid(price)

            except Exception as e:
                state["log"].insert(0, f"[{cn_time()}] 错误: {str(e)}")
        time.sleep(15)

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>OKX 模拟盘</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,sans-serif}
body{background:#f5f5f0;color:#1a1a1a;padding:16px}
h2{font-size:16px;font-weight:500;margin-bottom:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:14px}
@media(min-width:600px){.metrics{grid-template-columns:repeat(4,1fr)}}
.metric{background:#fff;border-radius:10px;padding:12px 14px;border:0.5px solid #e0e0d8}
.metric-label{font-size:11px;color:#888;margin-bottom:4px}
.metric-value{font-size:20px;font-weight:500}
.sub{font-size:11px;color:#aaa;margin-top:3px}
.up{color:#1D9E75}.down{color:#D85A30}.neutral{color:#1a1a1a}
.panel{background:#fff;border-radius:10px;padding:14px;border:0.5px solid #e0e0d8;margin-bottom:14px}
.panel-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.panel-header h3{font-size:12px;color:#888;font-weight:400}
.tab-group{display:flex;gap:4px}
.tab{font-size:11px;padding:3px 12px;border-radius:6px;border:0.5px solid #e0e0d8;background:#f5f5f0;color:#888;cursor:pointer}
.tab.active{background:#E1F5EE;color:#0F6E56;border-color:#9FE1CB;font-weight:500}
table{width:100%;font-size:12px;border-collapse:collapse}
th{text-align:left;color:#888;font-weight:400;padding:3px 6px 8px;border-bottom:0.5px solid #eee}
td{padding:5px 6px;border-bottom:0.5px solid #eee}
.badge{font-size:11px;padding:3px 10px;border-radius:6px;font-weight:500}
.live{background:#E1F5EE;color:#0F6E56}.offline{background:#f5f5f0;color:#888}
.log{font-size:11px;color:#666;font-family:monospace;max-height:120px;overflow-y:auto;line-height:1.9}
</style></head>
<body>
<h2>
  OKX 模拟盘 · 网格策略
  <span id="badge" class="badge offline">连接中</span>
  <span style="font-size:12px;color:#888;font-weight:400">BTC <span id="btcprice">—</span></span>
  <span style="font-size:11px;color:#bbb;margin-left:auto" id="starttime"></span>
</h2>
<div class="metrics">
  <div class="metric">
    <div class="metric-label">账户余额 (USDT)</div>
    <div class="metric-value neutral" id="balance">—</div>
    <div class="sub" id="initbal"></div>
  </div>
  <div class="metric">
    <div class="metric-label">累计真实盈亏</div>
    <div class="metric-value" id="pnl">—</div>
    <div class="sub" id="pnlpct"></div>
  </div>
  <div class="metric">
    <div class="metric-label">胜率（卖出笔数）</div>
    <div class="metric-value neutral" id="winrate">—</div>
  </div>
  <div class="metric">
    <div class="metric-label">最大回撤</div>
    <div class="metric-value down" id="drawdown">—</div>
  </div>
</div>
<div class="panel">
  <div class="panel-header">
    <h3>净值曲线</h3>
    <div class="tab-group">
      <div class="tab active" onclick="setTab(this,'hour')">小时</div>
      <div class="tab" onclick="setTab(this,'day')">日</div>
      <div class="tab" onclick="setTab(this,'month')">月</div>
    </div>
  </div>
  <div style="position:relative;height:160px"><canvas id="chart"></canvas></div>
</div>
<div class="panel">
  <div class="panel-header"><h3>成交记录</h3></div>
  <table><thead><tr><th>时间（北京）</th><th>方向</th><th>价格</th><th>数量</th><th>预估盈亏</th></tr></thead>
  <tbody id="trades"></tbody></table>
</div>
<div class="panel">
  <div class="panel-header"><h3>运行日志</h3></div>
  <div class="log" id="log">连接中...</div>
</div>
<script>
let chart=null, allHistory=[], currentTab='hour';
function setTab(el,tab){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active'); currentTab=tab; renderChart();
}
function aggregateHistory(data,mode){
  if(!data.length) return {labels:[],values:[]};
  const map=new Map();
  data.forEach(p=>{
    const [date,time]=p.t.split(' ');
    const [h]=time.split(':');
    let key = mode==='hour' ? date+' '+h+':00' : mode==='day' ? date : date.slice(0,7);
    if(!map.has(key)) map.set(key,[]);
    map.get(key).push(p.v);
  });
  const labels=[],values=[];
  map.forEach((vals,key)=>{
    labels.push(key);
    values.push(parseFloat((vals.reduce((a,b)=>a+b,0)/vals.length).toFixed(2)));
  });
  return {labels,values};
}
function renderChart(){
  const {labels,values}=aggregateHistory(allHistory,currentTab);
  if(!labels.length) return;
  if(!chart){
    chart=new Chart(document.getElementById('chart'),{
      type:'line',
      data:{labels,datasets:[{data:values,borderColor:'#1D9E75',backgroundColor:'rgba(29,158,117,0.08)',borderWidth:1.5,pointRadius:labels.length<30?3:0,fill:true,tension:0.3}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
        scales:{
          x:{ticks:{font:{size:10},color:'#aaa',maxRotation:30,autoSkip:true,maxTicksLimit:8},grid:{display:false}},
          y:{ticks:{font:{size:10},color:'#aaa',callback:v=>'$'+v.toFixed(0)},grid:{color:'rgba(0,0,0,0.05)'}}
        }}
    });
  } else {
    chart.data.labels=labels;
    chart.data.datasets[0].data=values;
    chart.data.datasets[0].pointRadius=labels.length<30?3:0;
    chart.update('none');
  }
}
async function refresh(){
  try{
    const d=await fetch('/api/state').then(r=>r.json());
    allHistory=d.pnl_history||[];
    document.getElementById('btcprice').textContent='$'+(d.price||0).toLocaleString();
    document.getElementById('balance').textContent='$'+d.balance.toFixed(2);
    document.getElementById('initbal').textContent='初始 $'+d.initial_balance.toFixed(2);
    document.getElementById('starttime').textContent=d.start_time?'启动于 '+d.start_time:'';
    const p=d.pnl;
    const pct=d.initial_balance>0?(p/d.initial_balance*100).toFixed(2):0;
    document.getElementById('pnl').className='metric-value '+(p>0?'up':p<0?'down':'neutral');
    document.getElementById('pnl').textContent=(p>=0?'+':'')+p.toFixed(2);
    document.getElementById('pnlpct').textContent=(p>=0?'+':'')+pct+'%';
    const sell_total=d.wins+d.losses;
    document.getElementById('winrate').textContent=sell_total>0?Math.round(d.wins/sell_total*100)+'% ('+sell_total+'笔)':'等待卖出';
    document.getElementById('drawdown').textContent=d.max_drawdown.toFixed(2)+'%';
    const badge=document.getElementById('badge');
    badge.textContent=d.api_connected?'实时运行':'连接中';
    badge.className='badge '+(d.api_connected?'live':'offline');
    document.getElementById('trades').innerHTML=d.trades.length
      ?d.trades.map(t=>`<tr><td>${t.time}</td><td style="color:${t.side==='买入'?'#1D9E75':'#D85A30'}">${t.side}</td><td>$${t.price.toLocaleString()}</td><td>${t.qty}</td><td style="color:${t.pnl>0?'#1D9E75':t.pnl<0?'#D85A30':'#888'}">${t.pnl>0?'+':''}${t.pnl}</td></tr>`).join('')
      :'<tr><td colspan="5" style="color:#aaa;text-align:center;padding:16px">等待交易信号...</td></tr>';
    document.getElementById('log').innerHTML=d.log.join('<br>')||'运行中...';
    renderChart();
  }catch(e){}
}
refresh(); setInterval(refresh,10000);
</script></body></html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/state")
def api_state():
    return jsonify(state)

@app.route("/api/reset")
def reset():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM meta; DELETE FROM pnl_history; DELETE FROM trades;")
        conn.commit(); cur.close(); conn.close()
    except: pass
    return jsonify({"ok": True, "msg": "已重置"})

if __name__ == "__main__":
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"启动成功！访问: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
