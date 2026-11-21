const API_BASE = "";

const TABLE_NAMES = [
    "market_prices",
    "market_sources",
    "news_articles",
    "economic_events",
    "system_logs"
];

let selectedResolution = "5";
let selectedSymbol = "";

let marketChart = null;
let candleSeries = null;
let marketRefreshTimer = null;
let liveSocket = null;
let resizeObserver = null;

let currentBars = [];
let oldestBarTime = null;
let historicalLoading = false;
let hasMoreHistory = true;
let reconnectTimer = null;

let isRestoringViewport = false;
let historyRequestId = 0;
let diagnosticsTimer = null;
let lastLiveUpdateTime = 0;
let lastHistoryError = "";

const $ = (id) => document.getElementById(id);


// ==================================================
// GENERAL HELPERS
// ==================================================

function getErrorMessage(detail, fallback) {
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object") {
        return detail.message || JSON.stringify(detail);
    }
    return fallback;
}

function updateClock() {
    if ($("clock")) {
        $("clock").textContent = new Date().toLocaleTimeString();
    }
}

function addActivity(title, description) {
    const list = $("activity-list");
    if (!list) return;

    const item = document.createElement("div");
    item.className = "activity-item";

    const dot = document.createElement("span");
    dot.className = "activity-dot";

    const content = document.createElement("div");
    const heading = document.createElement("strong");
    heading.textContent = title;

    const paragraph = document.createElement("p");
    paragraph.textContent = description;

    content.append(heading, paragraph);

    const time = document.createElement("small");
    time.textContent = new Date().toLocaleTimeString();

    item.append(dot, content, time);
    list.prepend(item);

    while (list.children.length > 8) {
        list.removeChild(list.lastChild);
    }
}

function setMarketStatus(text) {
    if ($("market-status")) $("market-status").textContent = text;
}

function setChartMessage(text) {
    if ($("chart-message")) $("chart-message").textContent = text;
}


// ==================================================
// HEALTH AND DATABASE
// ==================================================

async function checkHealth() {
    try {
        const response = await fetch(`${API_BASE}/health`, { cache: "no-store" });
        const data = await response.json();

        if (!response.ok) throw new Error("Backend health check failed");

        if ($("api-status")) $("api-status").textContent = "Online";
        if ($("database-status")) $("database-status").textContent = data.database || "Unknown";
        if ($("sidebar-status")) $("sidebar-status").textContent = "Connected";
        if ($("sidebar-status-dot")) $("sidebar-status-dot").style.background = "#35d69a";
    } catch (error) {
        if ($("api-status")) $("api-status").textContent = "Offline";
        if ($("database-status")) $("database-status").textContent = "Unknown";
        if ($("sidebar-status")) $("sidebar-status").textContent = "Disconnected";
        if ($("sidebar-status-dot")) $("sidebar-status-dot").style.background = "#ff6b75";
    }
}

async function loadDatabaseSummary() {
    try {
        const response = await fetch(`${API_BASE}/database-summary`, { cache: "no-store" });
        if (!response.ok) throw new Error("Database summary unavailable");

        const data = await response.json();
        const tableBody = $("database-table-body");
        if (!tableBody) return;

        tableBody.replaceChildren();

        let totalTables = 0;
        TABLE_NAMES.forEach((table) => {
            const row = document.createElement("tr");

            const nameCell = document.createElement("td");
            nameCell.textContent = table;

            const countCell = document.createElement("td");
            countCell.textContent = data.tables[table] ?? 0;

            const statusCell = document.createElement("td");
            statusCell.textContent = "Connected";

            row.append(nameCell, countCell, statusCell);
            tableBody.appendChild(row);
            totalTables++;
        });

        if ($("table-count")) $("table-count").textContent = totalTables;
        if ($("db-connection-label")) $("db-connection-label").textContent = "Connected";
    } catch (error) {
        if ($("db-connection-label")) $("db-connection-label").textContent = "Unavailable";
    }
}


// ==================================================
// CHART INITIALIZATION
// ==================================================

function initializeChart() {
    const container = $("market-chart");

    if (!container || !window.LightweightCharts) {
        throw new Error("Chart library could not load");
    }

    marketChart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: container.clientHeight,

        layout: {
            background: { color: "#0b1422" },
            textColor: "#aab7ca"
        },

        grid: {
            vertLines: { color: "#182438" },
            horzLines: { color: "#182438" }
        },

        rightPriceScale: {
            visible: true,
            borderColor: "#26364d",
            autoScale: true,
            scaleMargins: { top: 0.10, bottom: 0.10 }
        },

        timeScale: {
            borderColor: "#26364d",
            timeVisible: true,
            secondsVisible: false,
            rightOffset: 8,
            barSpacing: 7,
            minBarSpacing: 2,
            shiftVisibleRangeOnNewBar: false
        },

        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal
        },

        handleScroll: {
            mouseWheel: true,
            pressedMouseMove: true,
            horzTouchDrag: true,
            vertTouchDrag: true
        },

        handleScale: {
            mouseWheel: true,
            pinch: true,
            axisPressedMouseMove: true
        }
    });

    candleSeries = marketChart.addCandlestickSeries({
        upColor: "#35d69a",
        downColor: "#ff6b75",
        borderUpColor: "#35d69a",
        borderDownColor: "#ff6b75",
        wickUpColor: "#35d69a",
        wickDownColor: "#ff6b75",
        priceLineVisible: true,
        lastValueVisible: true
    });

    marketChart.timeScale().subscribeVisibleLogicalRangeChange(handleVisibleRangeChange);

    if (window.ResizeObserver) {
        resizeObserver = new ResizeObserver(() => {
            if (!marketChart) return;
            marketChart.applyOptions({
                width: container.clientWidth,
                height: container.clientHeight
            });
        });
        resizeObserver.observe(container);
    }
}


// ==================================================
// LOAD SYMBOLS
// ==================================================

async function loadGoldSymbols() {
    const select = $("market-symbol");

    try {
        const response = await fetch(`${API_BASE}/api/market/symbols`, { cache: "no-store" });
        const data = await response.json();

        if (!response.ok) {
            throw new Error(getErrorMessage(data.detail, "Could not load symbols"));
        }

        const activeSymbols = (data.symbols || []).filter(
            item => item.symbol && item.state === "stable"
        );

        if (!activeSymbols.length) throw new Error("No active Gold feed was found");

        if (select) {
            select.replaceChildren();
            activeSymbols.forEach((item) => {
                const option = document.createElement("option");
                option.value = item.symbol;
                option.textContent = item.name || item.symbol;
                select.appendChild(option);
            });
        }

        selectedSymbol = select ? select.value : activeSymbols[0].symbol;
        await loadMarketHistory(true);
        await loadResearchAnalysis();
    } catch (error) {
        if (select) {
            select.replaceChildren();
            const option = document.createElement("option");
            option.textContent = "Gold feed unavailable";
            option.value = "";
            select.appendChild(option);
        }
        setMarketStatus("Feed unavailable");
        setChartMessage(error.message);
        addActivity("Market data error", error.message);
    }
}


// ==================================================
// LOAD INITIAL HISTORICAL CANDLES
// ==================================================

async function loadMarketHistory(connectStream = false) {
    if (!selectedSymbol) return;

    const requestId = ++historyRequestId;

    setMarketStatus("Loading history...");
    setChartMessage("Loading historical candles...");

    historicalLoading = false;
    hasMoreHistory = true;
    currentBars = [];
    oldestBarTime = null;

    try {
        const params = new URLSearchParams({
            symbol: selectedSymbol,
            resolution: selectedResolution,
            bars: "500"
        });

        const response = await fetch(
            `${API_BASE}/api/market/history?${params.toString()}`,
            { cache: "no-store" }
        );
        const data = await response.json();

        if (!response.ok) {
            throw new Error(getErrorMessage(data.detail, "Market history failed"));
        }

        if (requestId !== historyRequestId) return;

        if (!Array.isArray(data.bars) || data.bars.length === 0) {
            throw new Error("Pyth returned zero historical candles for this range");
        }

        if (!marketChart) initializeChart();

        currentBars = data.bars
            .filter(bar => Number.isFinite(Number(bar.time)))
            .slice()
            .sort((a, b) => a.time - b.time);

        candleSeries.setData(currentBars);
        oldestBarTime = currentBars[0].time;

        // A short first page does not prove history has ended.
        // We keep trying older pages until the provider returns no older candles.
        hasMoreHistory = currentBars.length > 0;

        marketChart.timeScale().fitContent();

        const latest = currentBars[currentBars.length - 1];
        if ($("market-price")) $("market-price").textContent = Number(latest.close).toFixed(2);

        setMarketStatus("History loaded");
        setChartMessage("");

        if ($("market-updated")) {
            $("market-updated").textContent = `History: ${currentBars.length} candles`;
        }
        if ($("market-source")) {
            $("market-source").textContent = `Source: Pyth · ${data.symbol}`;
        }

        addActivity(
            "Historical chart loaded",
            `${data.count} candles returned · oldest ${new Date(oldestBarTime * 1000).toLocaleString()}`
        );

        if (connectStream) connectLiveStream();

        // If provider returned fewer bars than requested, show the exact count.
        if (data.count < 500) {
            setChartMessage(
                `Pyth returned ${data.count} candles for this request. Pan left to request older data.`
            );
        }
    } catch (error) {
        if (requestId !== historyRequestId) return;
        setMarketStatus("Data unavailable");
        setChartMessage(error.message);
        addActivity("Market chart error", error.message);
    }
}


// ==================================================
// LOAD OLDER HISTORICAL CANDLES
// ==================================================

async function loadOlderHistory() {
    if (
        historicalLoading ||
        !hasMoreHistory ||
        !oldestBarTime ||
        !selectedSymbol ||
        !candleSeries ||
        !marketChart
    ) return;

    historicalLoading = true;

    const previousRange = marketChart.timeScale().getVisibleLogicalRange();
    const oldLength = currentBars.length;
    const requestSymbol = selectedSymbol;
    const requestResolution = selectedResolution;
    const requestOldestTime = oldestBarTime;
    const requestId = historyRequestId;

    setChartMessage("Loading older candles...");

    try {
        const params = new URLSearchParams({
            symbol: requestSymbol,
            resolution: requestResolution,
            bars: "500",
            to: String(requestOldestTime - 1)
        });

        const response = await fetch(
            `${API_BASE}/api/market/history?${params.toString()}`,
            { cache: "no-store" }
        );
        const data = await response.json();

        if (!response.ok) {
            throw new Error(getErrorMessage(data.detail, "Older history request failed"));
        }

        if (
            requestId !== historyRequestId ||
            requestSymbol !== selectedSymbol ||
            requestResolution !== selectedResolution ||
            requestOldestTime !== oldestBarTime
        ) return;

        const olderBars = (data.bars || [])
            .filter(bar => Number(bar.time) < requestOldestTime)
            .sort((a, b) => a.time - b.time);

        if (!olderBars.length) {
            hasMoreHistory = false;
            setChartMessage(
                `No older candles returned by Pyth. Current oldest candle: ${new Date(oldestBarTime * 1000).toLocaleString()}`
            );
            return;
        }

        const merged = [...olderBars, ...currentBars];
        const unique = new Map();

        merged.forEach(bar => unique.set(Number(bar.time), bar));

        currentBars = Array.from(unique.values()).sort((a, b) => a.time - b.time);

        const added = currentBars.length - oldLength;
        if (added <= 0) {
            hasMoreHistory = false;
            setChartMessage("Pyth did not return any additional older candles.");
            return;
        }

        isRestoringViewport = true;
        candleSeries.setData(currentBars);
        oldestBarTime = currentBars[0].time;

        if (previousRange) {
            marketChart.timeScale().setVisibleLogicalRange({
                from: previousRange.from + added,
                to: previousRange.to + added
            });
        }

        setChartMessage(
            `${added} older candles loaded · ${currentBars.length} total`
        );

        if ($("market-updated")) {
            $("market-updated").textContent = `${currentBars.length} candles loaded`;
        }

        requestAnimationFrame(() => {
            isRestoringViewport = false;
        });

        // Keep loading available; the next page will confirm the history boundary.
        hasMoreHistory = true;
    } catch (error) {
        lastHistoryError = error.message;
        setChartMessage(`Older history error: ${error.message}`);
        console.error("Older history error:", error);
    } finally {
        historicalLoading = false;
    }
}


function handleVisibleRangeChange(range) {
    if (!range || historicalLoading || !hasMoreHistory || isRestoringViewport) return;

    // Trigger pagination when the visible chart approaches its left edge.
    if (range.from < 25) {
        loadOlderHistory();
    }
}


// ==================================================
// LIVE WEBSOCKET
// ==================================================

function closeLiveStream() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }

    if (liveSocket) {
        liveSocket.onclose = null;
        liveSocket.onerror = null;
        liveSocket.onmessage = null;
        liveSocket.close();
        liveSocket = null;
    }
}

function connectLiveStream() {
    if (!selectedSymbol) return;

    if (
        liveSocket &&
        (
            liveSocket.readyState === WebSocket.OPEN ||
            liveSocket.readyState === WebSocket.CONNECTING
        )
    ) return;

    closeLiveStream();

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url =
        `${protocol}//${window.location.host}/ws/market` +
        `?symbol=${encodeURIComponent(selectedSymbol)}`;

    liveSocket = new WebSocket(url);
    setMarketStatus("Connecting live feed...");

    liveSocket.onopen = () => {
        setMarketStatus("Live socket open; waiting for Pyth stream");
        addActivity("Live stream connected", `${selectedSymbol} · Pyth WebSocket`);
    };

    liveSocket.onmessage = (event) => {
        let message;
        try {
            message = JSON.parse(event.data);
        } catch {
            return;
        }

        if (message.type === "connection") {
            if (message.status === "connected") setMarketStatus("Pyth endpoint connected");
            if (message.status === "reconnecting") setMarketStatus("Pyth reconnecting...");
            return;
        }

        if (message.type === "heartbeat") return;

        if (message.type === "error") {
            setMarketStatus("Stream error");
            setChartMessage(message.message || "Live stream error");
            return;
        }

        if (message.type === "price") updateLiveCandle(message);
    };

    liveSocket.onerror = () => setMarketStatus("Live connection issue");

    liveSocket.onclose = () => {
        setMarketStatus("Live feed disconnected");
        if (document.visibilityState !== "hidden") {
            reconnectTimer = setTimeout(connectLiveStream, 4000);
        }
    };
}


// ==================================================
// LIVE CANDLE UPDATE
// ==================================================

function updateLiveCandle(message) {
    if (!candleSeries || !currentBars.length || !marketChart) return;

    const mantissa = Number(message.price);
    const exponent = Number(message.exponent);

    if (!Number.isFinite(mantissa) || !Number.isFinite(exponent)) return;

    const price = mantissa * Math.pow(10, exponent);
    if (!Number.isFinite(price) || price <= 0) return;

    // Use stream timestamp for candle placement.
    // feedUpdateTimestamp is only a freshness field, not the candle clock.
    const timestampUs = Number(message.timestampUs);
    if (!Number.isFinite(timestampUs) || timestampUs <= 0) return;

    const timestampSeconds = Math.floor(timestampUs / 1000000);
    const secondsPerCandle = Number(selectedResolution) * 60;
    const candleTime = Math.floor(timestampSeconds / secondsPerCandle) * secondsPerCandle;

    const latest = currentBars[currentBars.length - 1];
    if (candleTime < latest.time) return;

    const visibleRange = marketChart.timeScale().getVisibleLogicalRange();
    const wasFollowingLive = !visibleRange || visibleRange.to >= currentBars.length - 2;

    let nextCandle;

    if (candleTime === latest.time) {
        nextCandle = {
            time: latest.time,
            open: latest.open,
            high: Math.max(latest.high, price),
            low: Math.min(latest.low, price),
            close: price
        };
        currentBars[currentBars.length - 1] = nextCandle;
    } else {
        nextCandle = {
            time: candleTime,
            open: latest.close,
            high: Math.max(latest.close, price),
            low: Math.min(latest.close, price),
            close: price
        };
        currentBars.push(nextCandle);

        // Move only when the user was already following the live edge.
        if (wasFollowingLive) {
            marketChart.timeScale().scrollToRealTime();
        }
    }

    candleSeries.update(nextCandle);

    lastLiveUpdateTime = Date.now();

    if ($("market-price")) $("market-price").textContent = price.toFixed(2);
    setMarketStatus("Live · Pyth");

    if ($("market-updated")) {
        $("market-updated").textContent =
            `Stream: ${new Date(timestampSeconds * 1000).toLocaleTimeString()}`;
    }
    if ($("last-updated")) {
        $("last-updated").textContent = new Date().toLocaleTimeString();
    }
}


// ==================================================
// PYTH STREAM DIAGNOSTICS PANEL
// ==================================================

function createDiagnosticsPanel() {
    if ($("pyth-diagnostics-panel")) return;

    const chart = $("market-chart");
    if (!chart || !chart.parentElement) return;

    const panel = document.createElement("section");
    panel.id = "pyth-diagnostics-panel";
    panel.innerHTML = `
        <div class="pyth-diag-header">
            <div>
                <strong>PYTH LIVE DIAGNOSTICS</strong>
                <small>WebSocket and historical data monitor</small>
            </div>
            <span id="pyth-diag-overall">Waiting...</span>
        </div>
        <div class="pyth-diag-grid">
            <div class="pyth-diag-card"><small>Connected endpoints</small><strong id="diag-active">0 / 3</strong></div>
            <div class="pyth-diag-card"><small>Messages / minute</small><strong id="diag-messages">0</strong></div>
            <div class="pyth-diag-card"><small>Price changes / minute</small><strong id="diag-changes">0</strong></div>
            <div class="pyth-diag-card"><small>Chart candles</small><strong id="diag-candles">0</strong></div>
        </div>
        <div id="pyth-diag-endpoints"></div>
        <div class="pyth-diag-history" id="pyth-diag-history"></div>
        <small class="pyth-diag-note">
            Counts are per endpoint. The same provider update may arrive through multiple redundant endpoints.
            Counters reset when the Railway process restarts.
        </small>
    `;

    const style = document.createElement("style");
    style.textContent = `
        #pyth-diagnostics-panel{margin-top:16px;padding:18px;background:#0b1422;border:1px solid #26364d;border-radius:12px;color:#dce6f5;font-family:inherit}
        .pyth-diag-header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}
        .pyth-diag-header strong{display:block;font-size:13px;letter-spacing:1px}
        .pyth-diag-header small,.pyth-diag-note{color:#8ea0b9;font-size:11px}
        #pyth-diag-overall{font-size:12px;color:#35d69a}
        .pyth-diag-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}
        .pyth-diag-card{padding:12px;background:#111e30;border:1px solid #26364d;border-radius:8px;min-width:0}
        .pyth-diag-card small{display:block;color:#8ea0b9;font-size:11px;margin-bottom:7px}
        .pyth-diag-card strong{font-size:17px;overflow-wrap:anywhere}
        .pyth-endpoint{padding:12px;margin-top:8px;border:1px solid #26364d;background:#111e30;border-radius:8px;font-size:12px}
        .pyth-endpoint-title{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px}
        .pyth-endpoint-details{color:#aab7ca;line-height:1.8;overflow-wrap:anywhere}
        .pyth-diag-history{margin-top:12px;color:#aab7ca;font-size:12px;line-height:1.7}
        .pyth-diag-note{display:block;margin-top:12px;line-height:1.5}
        @media(max-width:700px){.pyth-diag-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
    `;

    document.head.appendChild(style);
    chart.parentElement.insertAdjacentElement("afterend", panel);
}

function formatDiagnosticTime(timestamp) {
    if (!timestamp) return "Never";
    return new Date(timestamp * 1000).toLocaleTimeString();
}

function renderPythDiagnostics(data) {
    const endpoints = data.endpoints || [];
    const active = endpoints.filter(item => item.status === "connected").length;
    const messages = endpoints.reduce((sum, item) => sum + (item.messages_per_minute || 0), 0);
    const changes = endpoints.reduce((sum, item) => sum + (item.price_changes_per_minute || 0), 0);

    if ($("diag-active")) $("diag-active").textContent = `${active} / ${endpoints.length}`;
    if ($("diag-messages")) $("diag-messages").textContent = messages;
    if ($("diag-changes")) $("diag-changes").textContent = changes;
    if ($("diag-candles")) $("diag-candles").textContent = currentBars.length;

    if ($("pyth-diag-overall")) {
        $("pyth-diag-overall").textContent =
            active ? `${active} endpoint(s) connected` : "No active endpoint";
    }

    const container = $("pyth-diag-endpoints");
    if (container) {
        container.replaceChildren();

        endpoints.forEach((item, index) => {
            const card = document.createElement("div");
            card.className = "pyth-endpoint";

            const title = document.createElement("div");
            title.className = "pyth-endpoint-title";

            const name = document.createElement("strong");
            name.textContent = `Endpoint ${index + 1}`;

            const status = document.createElement("span");
            status.textContent = `${item.status} · ${item.subscription_status}`;
            status.style.color =
                item.status === "connected" && item.subscription_status === "streaming"
                    ? "#35d69a" : "#ffbd69";

            title.append(name, status);

            const details = document.createElement("div");
            details.className = "pyth-endpoint-details";

            details.textContent = [
                `Messages/min: ${item.messages_per_minute ?? 0}`,
                `Price changes/min: ${item.price_changes_per_minute ?? 0}`,
                `Total messages: ${item.messages_received_total ?? 0}`,
                `Total price changes: ${item.price_changes_total ?? 0}`,
                `Last message: ${formatDiagnosticTime(item.last_message_at)}`,
                `Last price change: ${formatDiagnosticTime(item.last_price_change_at)}`,
                `Message age: ${item.message_age_seconds == null ? "Never" : `${item.message_age_seconds}s`}`,
                `Feed age estimate: ${item.feed_age_seconds_estimate == null ? "N/A" : `${item.feed_age_seconds_estimate}s`}`,
                `Last error: ${item.last_error || "None"}`
            ].join(" | ");

            card.append(title, details);
            container.appendChild(card);
        });
    }

    if ($("pyth-diag-history")) {
        $("pyth-diag-history").textContent =
            `History: ${currentBars.length} candles · oldest candle: ${
                oldestBarTime
                    ? new Date(oldestBarTime * 1000).toLocaleString()
                    : "not loaded"
            } · ${hasMoreHistory ? "older-page requests enabled" : "history boundary reached"}` +
            (lastHistoryError ? ` · last error: ${lastHistoryError}` : "");
    }
}

async function refreshPythDiagnostics() {
    try {
        const response = await fetch(
            `${API_BASE}/api/pyth/stream-diagnostics`,
            { cache: "no-store" }
        );
        if (!response.ok) throw new Error("Diagnostics endpoint unavailable");
        renderPythDiagnostics(await response.json());
    } catch (error) {
        if ($("pyth-diag-overall")) {
            $("pyth-diag-overall").textContent = "Diagnostics unavailable";
        }
    }
}

function startPythDiagnostics() {
    createDiagnosticsPanel();
    refreshPythDiagnostics();

    if (diagnosticsTimer) clearInterval(diagnosticsTimer);
    diagnosticsTimer = setInterval(refreshPythDiagnostics, 2000);
}


// ==================================================
// EDUCATIONAL MARKET RESEARCH
// ==================================================

let researchRequestId = 0;
let researchHistory = [];

function researchElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
}

function addResearchMetric(container, label, value, detail = "") {
    const card = researchElement("div", "research-metric");
    card.appendChild(researchElement("small", "", label));
    card.appendChild(researchElement("strong", "", value ?? "N/A"));
    if (detail) card.appendChild(researchElement("span", "", detail));
    container.appendChild(card);
}

function renderResearchHistory() {
    const container = $("research-history");
    if (!container) return;

    container.replaceChildren();
    if ($("research-history-count")) {
        $("research-history-count").textContent = researchHistory.length;
    }

    if (!researchHistory.length) {
        container.appendChild(
            researchElement("p", "research-empty", "এখনো কোনো analysis snapshot নেই।")
        );
        return;
    }

    researchHistory.forEach((item) => {
        const row = researchElement("div", "research-history-item");
        const info = researchElement("div");
        info.appendChild(researchElement("strong", "", item.symbol));
        info.appendChild(
            researchElement("span", "", `${item.available}/5 timeframe পাওয়া গেছে`)
        );
        row.appendChild(info);
        row.appendChild(
            researchElement("time", "", new Date(item.time).toLocaleTimeString())
        );
        container.appendChild(row);
    });
}

function renderResearchAnalysis(data) {
    const container = $("research-timeframes");
    if (!container) return;

    container.replaceChildren();

    (data.timeframes || []).forEach((timeframe) => {
        const card = researchElement("article", "research-timeframe-card");
        const head = researchElement("div", "research-timeframe-head");
        const heading = researchElement("div");
        heading.appendChild(researchElement("h3", "", timeframe.label));
        heading.appendChild(
            researchElement(
                "div",
                "research-snapshot-time",
                timeframe.available
                    ? `${timeframe.bar_count} historical candles`
                    : "ডেটা পাওয়া যায়নি"
            )
        );
        head.appendChild(heading);
        head.appendChild(
            researchElement(
                "span",
                "",
                timeframe.available ? "DATA READY" : "UNAVAILABLE"
            )
        );
        card.appendChild(head);

        if (!timeframe.available || !timeframe.indicators) {
            card.appendChild(
                researchElement(
                    "p",
                    "research-error",
                    timeframe.error || "এই timeframe-এর analysis তৈরি করা যায়নি।"
                )
            );
            container.appendChild(card);
            return;
        }

        const values = timeframe.indicators;
        const metrics = researchElement("div", "research-metrics-grid");

        addResearchMetric(
            metrics,
            "LATEST CLOSE",
            Number(values.close).toFixed(2),
            "সর্বশেষ সম্পন্ন/প্রাপ্ত candle-এর close"
        );
        addResearchMetric(
            metrics,
            "RSI (14)",
            values.rsi14 === null ? "N/A" : values.rsi14,
            values.rsi_label
        );
        addResearchMetric(
            metrics,
            "EMA 20",
            values.ema20 === null ? "N/A" : Number(values.ema20).toFixed(3),
            "২০-period weighted average"
        );
        addResearchMetric(
            metrics,
            "EMA 50",
            values.ema50 === null ? "N/A" : Number(values.ema50).toFixed(3),
            "৫০-period weighted average"
        );
        addResearchMetric(
            metrics,
            "MACD (12, 26, 9)",
            values.macd ? Number(values.macd.line).toFixed(5) : "N/A",
            values.macd ? values.macd.description : "পর্যাপ্ত candle নেই"
        );
        addResearchMetric(
            metrics,
            "BOLLINGER POSITION",
            values.bollinger ? values.bollinger.position : "N/A",
            values.bollinger
                ? `Upper ${values.bollinger.upper.toFixed(2)} · Middle ${values.bollinger.middle.toFixed(2)} · Lower ${values.bollinger.lower.toFixed(2)}`
                : "২০-period band"
        );
        addResearchMetric(
            metrics,
            "ATR (14)",
            values.atr14 === null ? "N/A" : Number(values.atr14).toFixed(3),
            values.volatility_description
        );
        addResearchMetric(
            metrics,
            "CANDLE BODY",
            `${values.candle_body_percent}%`,
            "সর্বশেষ candle-এর body / high-low range"
        );

        card.appendChild(metrics);

        const observations = researchElement("ul", "research-observations");
        (values.observations || []).forEach((observation) => {
            observations.appendChild(researchElement("li", "", observation));
        });
        card.appendChild(observations);
        container.appendChild(card);
    });

    const generatedAt = data.generated_at
        ? new Date(data.generated_at * 1000).toLocaleString()
        : new Date().toLocaleString();

    if ($("research-status")) {
        $("research-status").textContent =
            `${data.available_timeframes || 0}/5 timeframe · ${generatedAt}`;
    }

    researchHistory.unshift({
        symbol: data.symbol || selectedSymbol,
        available: data.available_timeframes || 0,
        time: Date.now(),
    });
    researchHistory = researchHistory.slice(0, 8);
    renderResearchHistory();
}

async function loadResearchAnalysis() {
    if (!selectedSymbol) return;

    const requestId = ++researchRequestId;
    const button = $("research-refresh");
    if (button) {
        button.disabled = true;
        button.textContent = "Analyzing data...";
    }
    if ($("research-status")) $("research-status").textContent = "Pyth history সংগ্রহ করা হচ্ছে...";

    try {
        const params = new URLSearchParams({ symbol: selectedSymbol });
        const response = await fetch(
            `${API_BASE}/api/market/analysis?${params.toString()}`,
            { cache: "no-store" }
        );
        const data = await response.json();

        if (!response.ok) {
            throw new Error(getErrorMessage(data.detail, "Research analysis unavailable"));
        }
        if (requestId !== researchRequestId) return;

        renderResearchAnalysis(data);
        addActivity(
            "Research snapshot updated",
            `${data.available_timeframes || 0}/5 timeframe · ${selectedSymbol}`
        );
    } catch (error) {
        if (requestId !== researchRequestId) return;
        if ($("research-status")) $("research-status").textContent = "Analysis unavailable";
        const container = $("research-timeframes");
        if (container) {
            container.replaceChildren(
                researchElement("p", "research-error", error.message)
            );
        }
    } finally {
        if (requestId === researchRequestId && button) {
            button.disabled = false;
            button.textContent = "↻ Refresh analysis";
        }
    }
}

function setupResearchControls() {
    const button = $("research-refresh");
    if (button) {
        button.addEventListener("click", loadResearchAnalysis);
    }
    renderResearchHistory();
}


// ==================================================
// MARKET CONTROLS
// ==================================================

function setupMarketControls() {
    const symbolSelect = $("market-symbol");

    if (symbolSelect) {
        symbolSelect.addEventListener("change", async (event) => {
            selectedSymbol = event.target.value;
            closeLiveStream();
            await loadMarketHistory(true);
            await loadResearchAnalysis();
        });
    }

    document.querySelectorAll(".timeframe-btn").forEach((button) => {
        button.addEventListener("click", async () => {
            const nextResolution = button.dataset.resolution;
            if (!nextResolution || nextResolution === selectedResolution) return;

            document.querySelectorAll(".timeframe-btn").forEach(item => item.classList.remove("active"));
            button.classList.add("active");

            selectedResolution = nextResolution;
            await loadMarketHistory(false);
        });
    });
}


// ==================================================
// DASHBOARD REFRESH
// ==================================================

async function refreshDashboard() {
    await Promise.all([checkHealth(), loadDatabaseSummary()]);

    if ($("last-updated")) {
        $("last-updated").textContent = new Date().toLocaleTimeString();
    }
}

function startDashboardRefresh() {
    if (marketRefreshTimer) clearInterval(marketRefreshTimer);
    marketRefreshTimer = setInterval(refreshDashboard, 30000);
}


// ==================================================
// PAGE VISIBILITY
// ==================================================

document.addEventListener("visibilitychange", () => {
    if (
        document.visibilityState === "visible" &&
        (!liveSocket || liveSocket.readyState === WebSocket.CLOSED)
    ) {
        connectLiveStream();
    }
});


// ==================================================
// INITIALIZATION
// ==================================================

async function initializeDashboard() {
    updateClock();
    setInterval(updateClock, 1000);

    setupMarketControls();
    setupResearchControls();

    await checkHealth();
    await loadDatabaseSummary();
    await loadGoldSymbols();

    if ($("last-updated")) {
        $("last-updated").textContent = new Date().toLocaleTimeString();
    }

    const refreshButton = $("refresh-button");
    if (refreshButton) {
        refreshButton.addEventListener("click", refreshDashboard);
    }

    startDashboardRefresh();
    startPythDiagnostics();
}

document.addEventListener("DOMContentLoaded", initializeDashboard);


// ==================================================
// GEMINI AI RESEARCH CARD (AI Research Center)
// ==================================================

const AI_BASE = (typeof API_BASE === "string") ? API_BASE : "";

function geminiEl(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = text;
    return el;
}

async function loadGeminiResearch() {
    const body = document.getElementById("ai-gemini-body");
    const meta = document.getElementById("ai-gemini-meta");
    if (!body) return;

    try {
        const [statusRes, researchRes] = await Promise.all([
            fetch(`${AI_BASE}/api/ai/status`),
            fetch(`${AI_BASE}/api/ai/research/latest`),
        ]);
        if (!statusRes.ok || !researchRes.ok) throw new Error("AI API unavailable");

        const status = await statusRes.json();
        const { research } = await researchRes.json();

        if (meta) {
            meta.textContent =
                `Gemini: ${status.gemini_configured ? `on (${status.model})` : "no API key"}`
                + ` · auto: ${status.auto ? `every ${status.interval_minutes} min` : "off"}`
                + (status.market_closed ? " · market closed" : "");
        }

        body.replaceChildren();
        if (!research) {
            body.appendChild(geminiEl("p", "research-empty", "এখনো কোনো AI রিসার্চ নেই। 'Run Gemini AI' চাপুন।"));
            return;
        }

        const colors = { BUY: "#22c55e", SELL: "#ef4444", NEUTRAL: "#8a96b0" };
        const head = geminiEl("p", "", "");
        const badge = geminiEl("strong", "", research.bias || "NEUTRAL");
        badge.style.color = colors[research.bias] || colors.NEUTRAL;
        head.appendChild(badge);
        head.appendChild(document.createTextNode(
            ` · ${research.confidence ?? 0}% · ${research.model} · ${new Date(research.created_at * 1000).toLocaleString()}`
        ));
        body.appendChild(head);
        body.appendChild(geminiEl("p", "", research.summary || ""));
        if (research.risk_note) {
            body.appendChild(geminiEl("p", "research-empty", `⚠ ${research.risk_note}`));
        }
    } catch (error) {
        body.replaceChildren(geminiEl("p", "research-error", error.message));
    }
}

async function runGeminiResearch() {
    const button = document.getElementById("ai-gemini-run");
    const meta = document.getElementById("ai-gemini-meta");
    if (button) { button.disabled = true; button.textContent = "Running..."; }

    let message = "";
    try {
        const response = await fetch(`${AI_BASE}/api/ai/run`, { method: "POST" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json();
        if (result.status === "cooldown") message = `wait ${result.wait_seconds}s`;
        else if (result.status === "busy") message = "previous run in progress";
        else if (result.status === "market_closed") message = "market closed, no new signal";
        else message = result.gemini_error ? result.gemini_error : "done";
    } catch (error) {
        message = `failed: ${error.message}`;
    }

    await loadGeminiResearch();
    if (meta && message) meta.textContent += ` · ${message}`;
    if (button) { button.disabled = false; button.textContent = "▶ Run Gemini AI"; }
}

function setupGeminiResearch() {
    const button = document.getElementById("ai-gemini-run");
    if (button) button.addEventListener("click", runGeminiResearch);
    loadGeminiResearch();
    setInterval(loadGeminiResearch, 60000);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setupGeminiResearch);
} else {
    setupGeminiResearch();
}


// ==================================================
// PAPER TRADES ON THE MAIN CHART
// ==================================================

let paperSeries = [];
let paperPriceLines = [];
let paperOverlayEnabled = true;

function clearPaperOverlay() {
    paperSeries.forEach((item) => {
        try { marketChart.removeSeries(item); } catch (error) { /* already removed */ }
    });
    paperPriceLines.forEach((line) => {
        try { candleSeries.removePriceLine(line); } catch (error) { /* already removed */ }
    });
    paperSeries = [];
    paperPriceLines = [];
    paperMarkers = [];
    applyChartMarkers();
}

async function refreshPaperOverlay() {
    if (!candleSeries || !marketChart) return;
    if (!paperOverlayEnabled) { clearPaperOverlay(); return; }

    try {
        const response = await fetch(`${AI_BASE}/api/ai/signals?limit=12`, { cache: "no-store" });
        if (!response.ok) return;
        const { signals } = await response.json();

        clearPaperOverlay();

        const step = Number(selectedResolution) * 60;
        const align = (time) => time - (time % step);
        const lastBar = currentBars.length ? currentBars[currentBars.length - 1].time : null;
        const markers = [];

        signals.forEach((signal) => {
            const isBuy = signal.direction === "BUY";
            const isOpen = signal.status === "OPEN";
            const startTime = align(signal.created_at);
            let endTime = isOpen ? lastBar : align(signal.closed_at || signal.created_at);
            if (endTime === null || endTime <= startTime) endTime = startTime + step;

            const levels = [
                [signal.entry, "#f5b942", "ENTRY"],
                [signal.stop_loss, "#ef4444", "SL"],
                [signal.take_profit, "#22c55e", "TP"],
            ];

            levels.forEach(([price, color, label]) => {
                const line = marketChart.addLineSeries({
                    color,
                    lineWidth: 1,
                    lineStyle: 2,
                    priceLineVisible: false,
                    lastValueVisible: false,
                    crosshairMarkerVisible: false,
                });
                line.setData([
                    { time: startTime, value: Number(price) },
                    { time: endTime, value: Number(price) },
                ]);
                paperSeries.push(line);

                if (isOpen) {
                    paperPriceLines.push(candleSeries.createPriceLine({
                        price: Number(price),
                        color,
                        lineVisible: false,
                        axisLabelVisible: true,
                        title: `${signal.strategy} ${label}`,
                    }));
                }
            });

            markers.push({
                time: startTime,
                position: isBuy ? "belowBar" : "aboveBar",
                color: isBuy ? "#22c55e" : "#ef4444",
                shape: isBuy ? "arrowUp" : "arrowDown",
                text: `${signal.direction} ${signal.strategy}`,
            });

            if (!isOpen) {
                const won = signal.status === "WIN";
                const lost = signal.status === "LOSS";
                markers.push({
                    time: endTime,
                    position: "inBar",
                    color: won ? "#22c55e" : lost ? "#ef4444" : "#8a96b0",
                    shape: "circle",
                    text: signal.status,
                });
            }
        });

        markers.sort((a, b) => a.time - b.time);
        paperMarkers = markers;
        applyChartMarkers();
    } catch (error) {
        console.warn("Paper overlay unavailable:", error);
    }
}

function setupPaperOverlay() {
    const container = document.getElementById("market-chart");
    if (container && container.parentElement && !document.getElementById("paper-toggle")) {
        const button = document.createElement("button");
        button.id = "paper-toggle";
        button.type = "button";
        button.className = "research-refresh-btn";
        button.style.margin = "0 0 8px";
        button.textContent = "Paper trades: ON";
        button.addEventListener("click", () => {
            paperOverlayEnabled = !paperOverlayEnabled;
            button.textContent = `Paper trades: ${paperOverlayEnabled ? "ON" : "OFF"}`;
            refreshPaperOverlay();
        });
        container.parentElement.insertBefore(button, container);
    }
    document.querySelectorAll(".timeframe-btn").forEach((button) => {
        button.addEventListener("click", () => setTimeout(refreshPaperOverlay, 2500));
    });
    setTimeout(refreshPaperOverlay, 4000);
    setInterval(refreshPaperOverlay, 20000);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setupPaperOverlay);
} else {
    setupPaperOverlay();
}


// ==================================================
// SMC ZONES ON THE MAIN CHART
// ==================================================

let smcSeries = [];
let smcMarkers = [];
let paperMarkers = [];
let smcOverlayEnabled = true;

function applyChartMarkers() {
    if (!candleSeries) return;
    const all = paperMarkers.concat(smcMarkers).sort((a, b) => a.time - b.time);
    try { candleSeries.setMarkers(all); } catch (error) { /* ignore marker errors */ }
}

function clearSmcOverlay() {
    smcSeries.forEach((item) => {
        try { marketChart.removeSeries(item); } catch (error) { /* already removed */ }
    });
    smcSeries = [];
    smcMarkers = [];
    applyChartMarkers();
}

async function refreshSmcOverlay() {
    if (!candleSeries || !marketChart) return;
    if (!smcOverlayEnabled) { clearSmcOverlay(); return; }

    try {
        const response = await fetch(`${AI_BASE}/api/ai/smc/zones`, { cache: "no-store" });
        if (!response.ok) return;
        const data = await response.json();

        clearSmcOverlay();

        const step = Number(selectedResolution) * 60;
        const align = (time) => time - (time % step);
        const lastBar = currentBars.length ? currentBars[currentBars.length - 1].time : null;
        if (lastBar === null) return;

        const styles = {
            OB: { bull: "#22c55e", bear: "#ef4444", style: 0, width: 2 },
            FVG: { bull: "#38bdf8", bear: "#f97316", style: 2, width: 1 },
            SD: { bull: "#a3e635", bear: "#e879f9", style: 3, width: 1 },
        };

        (data.zones || []).forEach((zone) => {
            const look = styles[zone.kind] || styles.OB;
            const color = zone.direction === "bull" ? look.bull : look.bear;
            const start = zone.time;
            const end = lastBar > start ? lastBar : start + step;
            const label = `${zone.kind} ${zone.direction === "bull" ? "▲" : "▼"}`;

            [[zone.top, true], [zone.bottom, false]].forEach(([price, labelled]) => {
                const line = marketChart.addLineSeries({
                    color,
                    lineWidth: look.width,
                    lineStyle: look.style,
                    priceLineVisible: false,
                    lastValueVisible: labelled,
                    title: labelled ? label : "",
                    crosshairMarkerVisible: false,
                });
                line.setData([
                    { time: start, value: Number(price) },
                    { time: end, value: Number(price) },
                ]);
                smcSeries.push(line);
            });
        });

        (data.events || []).forEach((event) => {
            smcMarkers.push({
                time: align(event.time),
                position: event.direction === 1 ? "belowBar" : "aboveBar",
                color: event.direction === 1 ? "#38bdf8" : "#f97316",
                shape: "square",
                text: `${event.kind} ${event.direction === 1 ? "▲" : "▼"}`,
            });
        });

        (data.sweeps || []).forEach((sweep) => {
            smcMarkers.push({
                time: align(sweep.time),
                position: sweep.direction === 1 ? "belowBar" : "aboveBar",
                color: "#facc15",
                shape: "circle",
                text: "SWEEP",
            });
        });

        applyChartMarkers();
    } catch (error) {
        console.warn("SMC overlay unavailable:", error);
    }
}

function setupSmcOverlay() {
    const container = document.getElementById("market-chart");
    if (container && container.parentElement && !document.getElementById("smc-toggle")) {
        const button = document.createElement("button");
        button.id = "smc-toggle";
        button.type = "button";
        button.className = "research-refresh-btn";
        button.style.margin = "0 0 8px 8px";
        button.textContent = "SMC zones: ON";
        button.addEventListener("click", () => {
            smcOverlayEnabled = !smcOverlayEnabled;
            button.textContent = `SMC zones: ${smcOverlayEnabled ? "ON" : "OFF"}`;
            refreshSmcOverlay();
        });
        container.parentElement.insertBefore(button, container);
    }
    document.querySelectorAll(".timeframe-btn").forEach((button) => {
        button.addEventListener("click", () => setTimeout(refreshSmcOverlay, 2800));
    });
    setTimeout(refreshSmcOverlay, 5000);
    setInterval(refreshSmcOverlay, 60000);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setupSmcOverlay);
} else {
    setupSmcOverlay();
}


// ==================================================
// PAPER TRADING MONITOR (live P/L under the chart)
// ==================================================

let paperMonitorData = null;

function paperMarketPrice() {
    if (typeof currentBars === "undefined" || !currentBars.length) return null;
    const price = Number(currentBars[currentBars.length - 1].close);
    return Number.isFinite(price) ? price : null;
}

function paperMoney(value) {
    const number = Number(value) || 0;
    return `${number >= 0 ? "+" : "-"}$${Math.abs(number).toFixed(2)}`;
}

function paperColor(value) {
    return Number(value) > 0 ? "#22c55e" : Number(value) < 0 ? "#ef4444" : "#8a96b0";
}

function paperCell(text, color, bold) {
    const cell = document.createElement("td");
    cell.textContent = text;
    cell.style.cssText = "padding:7px 6px;border-bottom:1px solid rgba(138,150,176,.2);white-space:nowrap";
    if (color) cell.style.color = color;
    if (bold) cell.style.fontWeight = "700";
    return cell;
}

function paperHeaderRow(labels) {
    const row = document.createElement("tr");
    labels.forEach((label) => {
        const cell = document.createElement("th");
        cell.textContent = label;
        cell.style.cssText = "text-align:left;padding:7px 6px;color:#8a96b0;font-weight:600;white-space:nowrap";
        row.appendChild(cell);
    });
    return row;
}

function paperTile(label, value, color) {
    const tile = document.createElement("div");
    const small = document.createElement("div");
    small.textContent = label;
    small.style.cssText = "font-size:12px;color:#8a96b0";
    const big = document.createElement("div");
    big.textContent = value;
    big.style.cssText = "font-size:20px;font-weight:800";
    if (color) big.style.color = color;
    tile.appendChild(small);
    tile.appendChild(big);
    return tile;
}

function renderPaperMonitor() {
    const summary = document.getElementById("paper-monitor-summary");
    const table = document.getElementById("paper-monitor-table");
    const sub = document.getElementById("paper-monitor-sub");
    if (!summary || !table || !paperMonitorData) return;

    const data = paperMonitorData;
    const price = paperMarketPrice();
    let floating = 0;

    const openRows = data.open_trades.map((trade) => {
        const direction = trade.direction === "BUY" ? 1 : -1;
        const pnl = price === null ? null : (price - trade.entry) * direction * trade.units;
        if (pnl !== null) floating += pnl;
        return { trade, pnl };
    });

    sub.textContent = `Virtual balance $${Number(data.start_balance).toFixed(0)} · risk ${data.risk_pct}% per trade · not real money`
        + (price === null ? "" : ` · price ${price.toFixed(2)}`);

    summary.replaceChildren(
        paperTile("Floating P/L (open)", price === null ? "--" : paperMoney(floating), paperColor(floating)),
        paperTile("Realized P/L (closed)", paperMoney(data.net_pnl), paperColor(data.net_pnl)),
        paperTile("Equity", `$${(Number(data.equity) + floating).toFixed(2)}`),
        paperTile("Open / Closed", `${data.open_count} / ${data.closed_count}`),
        paperTile("Win rate", data.win_rate === null ? "-" : `${data.win_rate}%`)
    );

    table.replaceChildren();

    if (!openRows.length && !data.recent_closed.length) {
        const row = document.createElement("tr");
        const cell = paperCell("এখনো কোনো পেপার ট্রেড নেই। সিগন্যাল তৈরি হলে এখানে দেখাবে।", "#8a96b0");
        row.appendChild(cell);
        table.appendChild(row);
        return;
    }

    if (openRows.length) {
        table.appendChild(paperHeaderRow(["চলমান", "Strategy", "Entry", "SL", "TP", "Now", "P/L ($)", "R"]));
        openRows.forEach(({ trade, pnl }) => {
            const row = document.createElement("tr");
            const isBuy = trade.direction === "BUY";
            row.appendChild(paperCell(trade.direction, isBuy ? "#22c55e" : "#ef4444", true));
            row.appendChild(paperCell(trade.strategy));
            row.appendChild(paperCell(Number(trade.entry).toFixed(2)));
            row.appendChild(paperCell(Number(trade.stop_loss).toFixed(2), "#ef4444"));
            row.appendChild(paperCell(Number(trade.take_profit).toFixed(2), "#22c55e"));
            row.appendChild(paperCell(price === null ? "--" : price.toFixed(2)));
            row.appendChild(paperCell(pnl === null ? "--" : paperMoney(pnl), paperColor(pnl), true));
            row.appendChild(paperCell(pnl === null ? "--" : `${(pnl / trade.risk_amount).toFixed(2)}R`, paperColor(pnl)));
            table.appendChild(row);
        });
    }

    if (data.recent_closed.length) {
        table.appendChild(paperHeaderRow(["বন্ধ", "Strategy", "Result", "P/L ($)", "Closed", "", "", ""]));
        data.recent_closed.forEach((trade) => {
            const row = document.createElement("tr");
            row.appendChild(paperCell(trade.direction, trade.direction === "BUY" ? "#22c55e" : "#ef4444", true));
            row.appendChild(paperCell(trade.strategy));
            row.appendChild(paperCell(trade.status, paperColor(trade.pnl)));
            row.appendChild(paperCell(paperMoney(trade.pnl), paperColor(trade.pnl), true));
            row.appendChild(paperCell(new Date(trade.closed_at * 1000).toLocaleString()));
            table.appendChild(row);
        });
    }
}

async function loadPaperMonitor() {
    try {
        const response = await fetch(`${AI_BASE}/api/ai/paper`, { cache: "no-store" });
        if (!response.ok) return;
        paperMonitorData = await response.json();
        renderPaperMonitor();
    } catch (error) {
        console.warn("Paper monitor unavailable:", error);
    }
}

function setupPaperMonitor() {
    loadPaperMonitor();
    setInterval(loadPaperMonitor, 15000);
    setInterval(renderPaperMonitor, 2000);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setupPaperMonitor);
} else {
    setupPaperMonitor();
}
