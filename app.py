"""
OKX 模拟盘网格交易面板 - Railway 部署版 v4
新增：时间坐标轴，按小时/日/月切换图表
"""

import hmac, hashlib, base64, time, json, threading, os
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, render_template_string
from flask_cors import CORS
import requests

API_KEY    = os.environ.get("OKX_API_KEY", "")
SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")

BASE_URL   = "https://www.okx.com"
SYMBOL     = "BTC-USDT"
GRID_LOW   = 66000
GRID_HIGH  = 73000
GRID_COUNT = 10
STATE_FILE = "/tmp/okx_state.json"

app = Flask(__name__)
CORS(app)

state = {
    "balance": 0,
    "initial_balance": 0,
    "pnl": 0.0,
    "trades": [],
    "price": 0,
    "wins": 0,
    "losses": 0,
    "total_trades": 0,
    "max_balance": 0,
    "max_drawdown": 0.0,
    "pnl_history": [],   # [{t: "2024-01-01 14:00", v: 5000.0}, ...]
    "grids": [],
    "log": [],
    "running": True,
    "api_connected": False,
    "start_time": ""
}

def cn_now():
    return datetime.now(timezone(timedelta(hours=8)))

def cn_time():
    return cn_now().strftime("%H:%M:%S")

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "initial_balance": state["initial_balance"],
                "max_balance":     state["max_balance"],
                "wins":            state["wins"],
                "losses":          state["losses"],
                "total_trades":    state["total_trades"],
                "trades":          state["trades"][:50],
                "pnl_history":     state["pnl_history"][-2000:],
                "start_time":      state["start_time"]
            }, f)
    except:
        pass

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                saved = json.load(f)
            state["initial_balance"] = saved.get("initial_balance", 0)
            state["max_balance"]     = saved.get("max_balance", 0)
            state["wins"]            = saved.get("wins", 0)
            state["losses"]          = saved.get("losses", 0)
            state["total_trades"]    = saved.get("total_trades", 0)
            state["trades"]          = saved.get("trades", [])
            state["pnl_history"]     = saved.get("pnl_history", [])
            state["start_time"]      = saved.get("start_time", "")
            return True
    except:
        pass
    return False

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
    data = okx_get("/api/v5/account/balance?ccy=USDT")
    try:
        return float(data["data"][0]["details"][0]["availBal"])
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
    state["trades"].insert(0, {
        "time": now, "side": side,
        "price": round(price), "qty": qty,
        "pnl": round(pnl_val, 2)
    })
    if len(state["trades"]) > 50:
        state["trades"].pop()
    state["total_trades"] += 1
    if side == "卖出":
        if pnl_val > 0: state["wins"] += 1
        else: state["losses"] += 1
    ok = "成功" if "data" in api_result else "失败"
    state["log"].insert(0, f"[{now}] {side} {qty} BTC @ ${round(price):,} → {ok}")
    if len(state["log"]) > 50:
        state["log"].pop()
    save_state()

def trading_loop():
    state["grids"] = build_grids()
    has_saved = load_state()
    if has_saved and state["initial_balance"] > 0:
        state["log"].insert(0, f"[{cn_time()}] 恢复历史数据，初始余额 ${state['initial_balance']:.2f}")
    else:
        state["log"].insert(0, f"[{cn_time()}] 首次启动，正在连接 OKX...")
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
                    save_state()
                    state["log"].insert(0, f"[{cn_time()}] 连接成功！初始余额 ${bal:.2f}")

                if state["initial_balance"] > 0:
                    state["pnl"] = round(bal - state["initial_balance"], 2)
                    if bal > state["max_balance"]:
                        state["max_balance"] = bal
                        save_state()
                    dd = (state["max_balance"] - bal) / state["max_balance"] * 100
                    state["max_drawdown"] = round(max(dd, 0), 2)

                # 记录带时间戳的净值历史
                state["pnl_history"].append({
                    "t": cn_now().strftime("%Y-%m-%d %H:%M"),
                    "v": round(bal, 2)
                })
                if len(state["pnl_history"]) > 2000:
                    state["pnl_history"].pop(0)

                if API_KEY:
                    run_grid(price)

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
.tab{font-size:11px;padding:3px 10px;border-radius:6px;border:0.5px solid #e0e0d8;background:#f5f5f0;color:#888;cursor:pointer}
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
    <div class="metric-label">胜率（卖出）</div>
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
let chart = null;
let allHistory = [];
let currentTab = 'hour';

function setTab(el, tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  currentTab = tab;
  renderChart();
}

function aggregateHistory(data, mode) {
  if (!data.length) return { labels: [], values: [] };
  const map = new Map();
  data.forEach(p => {
    let key;
    const [date, time] = p.t.split(' ');
    const [h] = time.split(':');
    if (mode === 'hour') key = date + ' ' + h + ':00';
    else if (mode === 'day') key = date;
    else key = date.slice(0, 7);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(p.v);
  });
  const labels = [], values = [];
  map.forEach((vals, key) => {
    labels.push(key);
    values.push(parseFloat((vals.reduce((a,b)=>a+b,0)/vals.length).toFixed(2)));
  });
  return { labels, values };
}

function renderChart() {
  const { labels, values } = aggregateHistory(allHistory, currentTab);
  if (!labels.length) return;
  if (!chart) {
    chart = new Chart(document.getElementById('chart'), {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: values,
          borderColor: '#1D9E75',
          backgroundColor: 'rgba(29,158,117,0.08)',
          borderWidth: 1.5,
          pointRadius: labels.length < 30 ? 3 : 0,
          fill: true,
          tension: 0.3
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: {
            ticks: {
              font: { size: 10 },
              color: '#aaa',
              maxRotation: 30,
              autoSkip: true,
              maxTicksLimit: 8
            },
            grid: { display: false }
          },
          y: {
            ticks: {
              font: { size: 10 },
              color: '#aaa',
              callback: v => '$' + v.toFixed(0)
            },
            grid: { color: 'rgba(0,0,0,0.05)' }
          }
        }
      }
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    chart.data.datasets[0].pointRadius = labels.length < 30 ? 3 : 0;
    chart.update('none');
  }
}

async function refresh() {
  try {
    const d = await fetch('/api/state').then(r => r.json());
    allHistory = d.pnl_history || [];

    document.getElementById('btcprice').textContent = '$' + (d.price || 0).toLocaleString();
    document.getElementById('balance').textContent = '$' + d.balance.toFixed(2);
    document.getElementById('initbal').textContent = '初始 $' + d.initial_balance.toFixed(2);
    document.getElementById('starttime').textContent = d.start_time ? '启动于 ' + d.start_time : '';

    const p = d.pnl;
    const pct = d.initial_balance > 0 ? (p / d.initial_balance * 100).toFixed(2) : 0;
    document.getElementById('pnl').className = 'metric-value ' + (p > 0 ? 'up' : p < 0 ? 'down' : 'neutral');
    document.getElementById('pnl').textContent = (p >= 0 ? '+' : '') + p.toFixed(2);
    document.getElementById('pnlpct').textContent = (p >= 0 ? '+' : '') + pct + '%';

    const sell_total = d.wins + d.losses;
    document.getElementById('winrate').textContent = sell_total > 0
      ? Math.round(d.wins / sell_total * 100) + '% (' + sell_total + '笔)'
      : '等待卖出';
    document.getElementById('drawdown').textContent = d.max_drawdown.toFixed(2) + '%';

    const badge = document.getElementById('badge');
    badge.textContent = d.api_connected ? '实时运行' : '连接中';
    badge.className = 'badge ' + (d.api_connected ? 'live' : 'offline');

    document.getElementById('trades').innerHTML = d.trades.length
      ? d.trades.map(t =>
          `<tr>
            <td>${t.time}</td>
            <td style="color:${t.side==='买入'?'#1D9E75':'#D85A30'}">${t.side}</td>
            <td>$${t.price.toLocaleString()}</td>
            <td>${t.qty}</td>
            <td style="color:${t.pnl>0?'#1D9E75':t.pnl<0?'#D85A30':'#888'}">${t.pnl>0?'+':''}${t.pnl}</td>
          </tr>`).join('')
      : '<tr><td colspan="5" style="color:#aaa;text-align:center;padding:16px">等待交易信号...</td></tr>';

    document.getElementById('log').innerHTML = d.log.join('<br>') || '运行中...';

    renderChart();
  } catch(e) {}
}

refresh();
setInterval(refresh, 10000);
</script>
</body></html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/state")
def api_state():
    return jsonify(state)

@app.route("/api/reset")
def reset():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    return jsonify({"ok": True, "msg": "已重置"})

if __name__ == "__main__":
    threading.Thread(target=trading_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print(f"启动成功！访问: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
