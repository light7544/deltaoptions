import os

file_path = r"c:\Users\abhin\OneDrive\Desktop\deltaoptions\delta_algo\dashboard\static\index.html"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add Lightweight Charts
content = content.replace(
    '<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>',
    '<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>\n<script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>'
)

# 2. Add Option Chain Button
content = content.replace(
    '<button onclick="openTradeModal()"',
    '<button onclick="openOptionChainModal()" class="w-full mb-3 py-3 rounded-lg bg-surface-container-high border border-outline-variant/30 text-primary-fixed font-bold text-xs tracking-widest hover:border-primary-fixed hover:shadow-[0_0_15px_rgba(0,255,163,0.1)] active:scale-95 transition-all">OPTION CHAIN</button>\n<button onclick="openTradeModal()"'
)

# 3. Replace Right Panel with Charts
right_panel_start = content.find('<!-- Right Panel: Massive Option Chain -->')
right_panel_end = content.find('</section>', right_panel_start) + len('</section>')
old_right_panel = content[right_panel_start:right_panel_end]

new_right_panel = """<!-- Right Panel: Advanced Charts -->
<section class="col-span-6 row-span-10 glass-panel border border-outline-variant/10 rounded flex flex-col overflow-hidden relative">
<div class="p-3 border-b border-outline-variant/10 bg-surface-container-low flex justify-between items-center z-10">
<div class="flex items-center gap-2">
<span class="text-[10px] font-label text-outline tracking-widest uppercase">Advanced Algo Chart</span>
<select id="chart-symbol-select" onchange="fetchChartData()" class="text-[10px] bg-surface-container px-2 py-0.5 rounded text-primary-fixed focus:outline-none focus:ring-1 focus:ring-primary-fixed cursor-pointer font-bold border-none appearance-none">
    <option value="BTCUSD" class="bg-surface">BTC/USD</option>
    <option value="ETHUSD" class="bg-surface">ETH/USD</option>
</select>
</div>
<div class="flex gap-4 font-mono text-[10px]" id="chart-signals-header">
</div>
</div>
<div id="tvchart" class="flex-1 w-full h-full relative p-0 m-0 overflow-hidden"></div>
</section>

<!-- Full Option Chain Modal -->
<div id="optionChainModal" class="hidden fixed inset-0 z-[150] items-center justify-center bg-[#050810]/90 backdrop-blur-xl">
    <div class="glass-panel border border-[#00FFA3]/30 rounded-xl p-6 w-[95vw] h-[90vh] shadow-[0_0_80px_rgba(0,255,163,0.15)] relative flex flex-col overflow-hidden">
        <button onclick="closeOptionChainModal()" class="absolute top-4 right-4 text-outline hover:text-primary-fixed transition-colors z-50">
            <span class="material-symbols-outlined">close</span>
        </button>
        <div class="flex items-center gap-4 mb-4">
            <h2 class="text-xl font-bold text-on-surface font-['Inter'] flex items-center gap-2">
                <span class="material-symbols-outlined text-primary-fixed">view_list</span>
                OPTION CHAIN MATRIX
            </h2>
            <select id="underlying-select" onchange="fetchOptionChain()" class="text-sm bg-surface-container px-3 py-1 rounded text-primary-fixed focus:outline-none focus:ring-1 focus:ring-primary-fixed cursor-pointer font-bold border border-outline-variant/30">
                <option value="BTC" class="bg-surface">BTC Options</option>
                <option value="ETH" class="bg-surface">ETH Options</option>
            </select>
            <span id="expiry-display" class="font-mono text-xs text-outline-variant"></span>
            
            <div class="ml-auto text-xs font-mono flex items-center gap-4 border border-outline-variant/30 px-4 py-2 rounded bg-surface-container-low text-on-surface" id="chain-sentiment-bar">
                Calculating sentiment...
            </div>
        </div>
        
        <div class="flex-1 overflow-auto rounded border border-outline-variant/20 bg-surface-container-lowest relative">
            <table class="w-full text-left font-mono text-[10px] border-collapse" style="white-space: nowrap;">
                <thead class="bg-surface-container-highest sticky top-0 z-10 text-[10px]">
                    <tr>
                        <th colspan="7" class="p-2 text-center border-b border-outline-variant/20 font-bold text-primary-fixed bg-surface-container-low/50 tracking-[0.2em] border-r">CALLS</th>
                        <th class="p-2 text-center border-b border-outline-variant/20 bg-surface-container-lowest border-r text-on-surface">STRIKE</th>
                        <th colspan="7" class="p-2 text-center border-b border-outline-variant/20 font-bold text-secondary bg-surface-container-low/50 tracking-[0.2em]">PUTS</th>
                    </tr>
                    <tr class="text-outline uppercase bg-surface-container-high border-b border-outline-variant/30">
                        <th class="p-2 font-normal text-right bg-primary-container/[0.05]">Δ (Del)</th>
                        <th class="p-2 font-normal text-right bg-primary-container/[0.05]">Γ (Gam)</th>
                        <th class="p-2 font-normal text-right bg-primary-container/[0.05]">ν (Veg)</th>
                        <th class="p-2 font-normal text-right bg-primary-container/[0.05]">ρ (Rho)</th>
                        <th class="p-2 font-normal text-right text-primary-fixed bg-primary-container/[0.1]">OI Chg</th>
                        <th class="p-2 font-normal text-right bg-primary-container/[0.05]">OI</th>
                        <th class="p-2 font-normal text-right bg-primary-container/[0.05] border-r border-outline-variant/30">MARK/IV</th>
                        
                        <th class="p-2 font-bold text-center bg-surface-container animate-pulse border-r border-outline-variant/30">STRIKE</th>
                        
                        <th class="p-2 font-normal text-left bg-secondary-container/[0.05]">MARK/IV</th>
                        <th class="p-2 font-normal text-left bg-secondary-container/[0.05]">OI</th>
                        <th class="p-2 font-normal text-left text-secondary bg-secondary-container/[0.1]">OI Chg</th>
                        <th class="p-2 font-normal text-left bg-secondary-container/[0.05]">ρ (Rho)</th>
                        <th class="p-2 font-normal text-left bg-secondary-container/[0.05]">ν (Veg)</th>
                        <th class="p-2 font-normal text-left bg-secondary-container/[0.05]">Γ (Gam)</th>
                        <th class="p-2 font-normal text-left bg-secondary-container/[0.05]">Δ (Del)</th>
                    </tr>
                </thead>
                <tbody id="chain-body" class="divide-y divide-outline-variant/10">
                    <tr><td colspan="15" class="text-center p-8 text-outline">Loading option chain...</td></tr>
                </tbody>
            </table>
        </div>
    </div>
</div>
"""
content = content[:right_panel_start] + new_right_panel + content[right_panel_end:]

# 4. Modals JS functions & Chart logic
js_insert_pos = content.find('function formatCurrency(val) {')
js_additions = """
        function openOptionChainModal() {
            document.getElementById('optionChainModal').classList.remove('hidden');
            document.getElementById('optionChainModal').classList.add('flex');
            fetchOptionChain();
        }
        function closeOptionChainModal() {
            document.getElementById('optionChainModal').classList.remove('flex');
            document.getElementById('optionChainModal').classList.add('hidden');
        }
        
        let chart, candleSeries;
        let lineT1, lineT2, lineT3, lineSL;
        
        function initChart() {
            if(chart) return;
            const container = document.getElementById('tvchart');
            if(!container) return;
            chart = LightweightCharts.createChart(container, {
                width: container.clientWidth,
                height: container.clientHeight,
                layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#b9cbbd' },
                grid: { vertLines: { color: 'rgba(58, 74, 63, 0.1)' }, horzLines: { color: 'rgba(58, 74, 63, 0.1)' } },
                crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
                rightPriceScale: { borderColor: 'rgba(58, 74, 63, 0.3)' },
                timeScale: { borderColor: 'rgba(58, 74, 63, 0.3)', timeVisible: true }
            });
            candleSeries = chart.addCandlestickSeries({
                upColor: '#00FFA3', downColor: '#ffb4ab', borderVisible: false,
                wickUpColor: '#00FFA3', wickDownColor: '#ffb4ab'
            });
            
            lineT1 = candleSeries.createPriceLine({price: 0, color: '#00e290', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'T1'});
            lineT2 = candleSeries.createPriceLine({price: 0, color: '#00ffa3', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'T2'});
            lineT3 = candleSeries.createPriceLine({price: 0, color: '#52ffac', lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: 'T3'});
            lineSL = candleSeries.createPriceLine({price: 0, color: '#ffb4ab', lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: 'SL'});
            
            window.addEventListener('resize', () => {
                chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
            });
        }
        
        async function fetchChartData() {
            try {
                if(!chart) initChart();
                const sym = document.getElementById('chart-symbol-select').value;
                const res = await fetch(`/api/chart-data/${sym}`);
                const result = await res.json();
                if(result.status === 'success') {
                    candleSeries.setData(result.data.candles);
                    const sig = result.data.signals;
                    const tr = result.data.trend;
                    lineT1.applyOptions({price: sig.T1, color: tr === 1 ? '#00e290' : '#ffb4ab'});
                    lineT2.applyOptions({price: sig.T2, color: tr === 1 ? '#00ffa3' : '#ffb4ab'});
                    lineT3.applyOptions({price: sig.T3, color: tr === 1 ? '#52ffac' : '#ffdad7'});
                    lineSL.applyOptions({price: sig.SL, color: tr === 1 ? '#ffb4ab' : '#00ffa3'});
                    
                    let trStr = tr === 1 ? '<span class="text-primary-fixed">BULLISH 🟢</span>' : (tr === -1 ? '<span class="text-secondary">BEARISH 🔴</span>' : '<span class="text-outline">NEUTRAL 🟡</span>');
                    document.getElementById('chart-signals-header').innerHTML = `
                        TREND: ${trStr} |
                        T1: ${formatCurrency(sig.T1)} |
                        T2: ${formatCurrency(sig.T2)} |
                        T3: ${formatCurrency(sig.T3)} |
                        SL: ${formatCurrency(sig.SL)}
                    `;
                }
            } catch(e) { console.error('Chart error', e); }
        }
"""
content = content[:js_insert_pos] + js_additions + content[js_insert_pos:]

# 5. Replace inside fetchOptionChain
old_fetch_option_chain_start = content.find('const tbody = document.getElementById(\'chain-body\');')
old_fetch_option_chain_end = content.find('const tfoot = null;', old_fetch_option_chain_start)

new_chain_js = """const tbody = document.getElementById('chain-body');
                    if (d.strikes.length === 0) {
                        tbody.innerHTML = `<tr><td colspan="15" class="empty-state text-center">No chain data</td></tr>`;
                        return;
                    }
                    
                    let atmStrike = d.strikes.reduce((prev, curr) => Math.abs(curr - d.spot) < Math.abs(prev - d.spot) ? curr : prev);

                    // Find top 3 OI changes
                    let allStrikesData = [];
                    d.strikes.forEach(strike => {
                        const call = d.calls[strike + ".0"] || d.calls[strike.toString()] || {};
                        const put  = d.puts[strike + ".0"] || d.puts[strike.toString()] || {};
                        allStrikesData.push({strike: strike, chg: Math.abs(call.oi_change || 0) + Math.abs(put.oi_change || 0)});
                    });
                    
                    allStrikesData.sort((a,b) => b.chg - a.chg);
                    const topStrikes = allStrikesData.slice(0,3).map(x => x.strike);

                    tbody.innerHTML = d.strikes.map(strike => {
                        const call = d.calls[strike + ".0"] || d.calls[strike.toString()] || {};
                        const put = d.puts[strike + ".0"] || d.puts[strike.toString()] || {};
                        const isAtm = strike === atmStrike;
                        const isTopStrike = topStrikes.includes(strike);
                        
                        const formatG = (v) => (v) ? (v).toFixed(4) : "0.0000";
                        const formatO = (v) => (v) ? (v).toFixed(2) : "0.00";
                        
                        let trClass = isAtm ? 'bg-primary-container/[0.05] border-y border-outline-variant/30 font-bold shadow-[0_0_20px_rgba(0,255,163,0.1)]' : 'hover:bg-surface-container-high/50 border-b border-outline-variant/10 text-on-surface';
                        if (isTopStrike && !isAtm) trClass += ' bg-surface-container shadow-[inset_0_0_10px_rgba(255,255,255,0.05)]';
                        
                        let strikeClass = isAtm ? 'text-primary-container bg-surface-container text-[12px]' : (isTopStrike ? 'text-on-surface bg-surface-container glow-strike shadow-[0_0_8px_inset_rgba(0,163,255,0.2)]' : 'text-outline-variant bg-surface-container-lowest');

                        return `<tr class="transition-colors ${trClass}">
                            <td class="p-2 text-right">${formatG(call.delta)}</td>
                            <td class="p-2 text-right">${formatG(call.gamma)}</td>
                            <td class="p-2 text-right">${formatG(call.vega)}</td>
                            <td class="p-2 text-right">${formatG(call.rho)}</td>
                            <td class="p-2 text-right font-bold text-primary-fixed">${formatO(call.oi_change)}</td>
                            <td class="p-2 text-right">${formatO(call.oi)}</td>
                            <td class="p-2 text-right border-r border-outline-variant/20">
                                <div>${formatCurrency(call.mark_price)}</div>
                                <div class="text-[8px] text-outline-variant">${formatO(call.mark_vol)}%</div>
                            </td>
                            
                            <td class="p-2 text-center border-r border-outline-variant/20 font-bold ${strikeClass}">${strike}</td>
                            
                            <td class="p-2 text-left">
                                <div>${formatCurrency(put.mark_price)}</div>
                                <div class="text-[8px] text-outline-variant">${formatO(put.mark_vol)}%</div>
                            </td>
                            <td class="p-2 text-left">${formatO(put.oi)}</td>
                            <td class="p-2 text-left font-bold text-secondary">${formatO(put.oi_change)}</td>
                            <td class="p-2 text-left">${formatG(put.rho)}</td>
                            <td class="p-2 text-left">${formatG(put.vega)}</td>
                            <td class="p-2 text-left">${formatG(put.gamma)}</td>
                            <td class="p-2 text-left">${formatG(put.delta)}</td>
                        </tr>`;
                    }).join('');
                    
                    let totalCallOI = 0, totalPutOI = 0;
                    let maxCallOI = {oi:0, strike:0}, maxPutOI = {oi:0, strike:0};
                    
                    d.strikes.forEach(strike => {
                        const call = d.calls[strike + ".0"] || d.calls[strike.toString()] || {};
                        const put = d.puts[strike + ".0"] || d.puts[strike.toString()] || {};
                        if(call.oi) { totalCallOI += call.oi; if(call.oi > maxCallOI.oi) maxCallOI = {oi:call.oi, strike}; }
                        if(put.oi) { totalPutOI += put.oi; if(put.oi > maxPutOI.oi) maxPutOI = {oi:put.oi, strike}; }
                    });
                    
                    let pcr = totalCallOI > 0 ? (totalPutOI / totalCallOI) : 1;
                    document.getElementById('chain-sentiment-bar').innerHTML = `
                        <span class="text-outline">P/C RATIO:</span> <span class="${pcr>1.2?'text-primary-fixed':(pcr<0.8?'text-secondary':'text-on-surface')}">${pcr.toFixed(2)}</span>
                        | <span class="text-outline">SUP:</span> <span class="text-primary-fixed">${formatCurrency(maxPutOI.strike)}</span>
                        | <span class="text-outline">RES:</span> <span class="text-secondary">${formatCurrency(maxCallOI.strike)}</span>
                    `;
                    
                    """
content = content[:old_fetch_option_chain_start] + new_chain_js + content[old_fetch_option_chain_end:]

# Add fetchChartData to initLoop and interval
content = content.replace(
    'fetchOptionChain();\n            fetchSignals();',
    'fetchChartData();\n            fetchSignals();'
)

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)
print("done")
