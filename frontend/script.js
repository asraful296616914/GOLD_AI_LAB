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
let initialChartLoad = true;
let reconnectTimer = null;

const $ = (id) => document.getElementById(id);


function getErrorMessage(detail, fallback) {
    if (typeof detail === "string") return detail;

    if (detail && typeof detail === "object") {
        return detail.message || JSON.stringify(detail);
    }

    return fallback;
}


function updateClock() {
    const now = new Date();

    if ($("clock")) {
        $("clock").textContent = now.toLocaleTimeString();
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
    if ($("market-status")) {
        $("market-status").textContent = text;
    }
}


async function checkHealth() {
    try {
        const response = await fetch(`${API_BASE}/health`);
        const data = await response.json();

        if (!response.ok) {
            throw new Error("Backend health check failed");
        }

        if ($("api-status")) {
            $("api-status").textContent = "Online";
        }

        if ($("database-status")) {
            $("database-status").textContent =
                data.database || "Unknown";
        }

        if ($("sidebar-status")) {
            $("sidebar-status").textContent = "Connected";
        }

        if ($("sidebar-status-dot")) {
            $("sidebar-status-dot").style.background = "#35d69a";
        }

    } catch (error) {
        if ($("api-status")) {
            $("api-status").textContent = "Offline";
        }

        if ($("database-status")) {
            $("database-status").textContent = "Unknown";
        }

        if ($("sidebar-status")) {
            $("sidebar-status").textContent = "Disconnected";
        }

        if ($("sidebar-status-dot")) {
            $("sidebar-status-dot").style.background = "#ff6b75";
        }
    }
}


async function loadDatabaseSummary() {
    try {
        const response = await fetch(
            `${API_BASE}/database-summary`
        );

        if (!response.ok) {
            throw new Error("Database summary unavailable");
        }

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

        if ($("table-count")) {
            $("table-count").textContent = totalTables;
        }

        if ($("db-connection-label")) {
            $("db-connection-label").textContent = "Connected";
        }

    } catch (error) {
        if ($("db-connection-label")) {
            $("db-connection-label").textContent = "Unavailable";
        }
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
            background: {
                color: "#0b1422"
            },
            textColor: "#aab7ca"
        },

        grid: {
            vertLines: {
                color: "#182438"
            },
            horzLines: {
                color: "#182438"
            }
        },

        rightPriceScale: {
            borderColor: "#26364d"
        },

        timeScale: {
            borderColor: "#26364d",
            timeVisible: true,
            secondsVisible: false,
            rightOffset: 8,
            barSpacing: 7,
            minBarSpacing: 2
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
        wickDownColor: "#ff6b75"
    });

    // Load older candles when the user scrolls to the left.
    marketChart.timeScale().subscribeVisibleLogicalRangeChange(
        handleVisibleRangeChange
    );

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
// LOAD GOLD SYMBOLS
// ==================================================

async function loadGoldSymbols() {
    const select = $("market-symbol");

    try {
        const response = await fetch(
            `${API_BASE}/api/market/symbols`
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                getErrorMessage(data.detail, "Could not load symbols")
            );
        }

        const activeSymbols = (data.symbols || []).filter(
            item => item.symbol && item.state === "stable"
        );

        if (!activeSymbols.length) {
            throw new Error("No active Gold feed was found");
        }

        if (select) {
            select.replaceChildren();

            activeSymbols.forEach((item) => {
                const option = document.createElement("option");
                option.value = item.symbol;
                option.textContent = item.name || item.symbol;

                select.appendChild(option);
            });
        }

        selectedSymbol = select
            ? select.value
            : activeSymbols[0].symbol;

        await loadMarketHistory(true);

    } catch (error) {
        if (select) {
            select.replaceChildren();

            const option = document.createElement("option");
            option.textContent = "Gold feed unavailable";
            option.value = "";

            select.appendChild(option);
        }

        setMarketStatus("Feed unavailable");

        if ($("chart-message")) {
            $("chart-message").textContent = error.message;
        }

        addActivity("Market data error", error.message);
    }
}


// ==================================================
// LOAD HISTORICAL CANDLES
// ==================================================

async function loadMarketHistory(connectStream = false) {
    if (!selectedSymbol) return;

    setMarketStatus("Loading history...");

    if ($("chart-message")) {
        $("chart-message").textContent = "Loading historical candles...";
    }

    try {
        const params = new URLSearchParams({
            symbol: selectedSymbol,
            resolution: selectedResolution,
            bars: "500"
        });

        const response = await fetch(
            `${API_BASE}/api/market/history?${params.toString()}`
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                getErrorMessage(data.detail, "Market history failed")
            );
        }

        if (!data.bars || data.bars.length === 0) {
            throw new Error("No historical candles available");
        }

        if (!marketChart) {
            initializeChart();
        }

        currentBars = data.bars
            .slice()
            .sort((a, b) => a.time - b.time);

        candleSeries.setData(currentBars);

        oldestBarTime = currentBars[0].time;
        hasMoreHistory = currentBars.length >= 500;

        // Fit only when a new chart/timeframe is loaded.
        marketChart.timeScale().fitContent();

        initialChartLoad = false;

        const latest = currentBars[currentBars.length - 1];

        if ($("market-price")) {
            $("market-price").textContent =
                Number(latest.close).toFixed(2);
        }

        if ($("market-status")) {
            $("market-status").textContent = "History loaded";
        }

        if ($("market-updated")) {
            $("market-updated").textContent =
                `History: ${currentBars.length} candles`;
        }

        if ($("market-source")) {
            $("market-source").textContent =
                `Source: Pyth · ${data.symbol}`;
        }

        if ($("chart-message")) {
            $("chart-message").textContent = "";
        }

        addActivity(
            "Historical chart loaded",
            `${data.symbol} · ${currentBars.length} candles`
        );

        if (connectStream) {
            connectLiveStream();
        }

    } catch (error) {
        setMarketStatus("Data unavailable");

        if ($("chart-message")) {
            $("chart-message").textContent = error.message;
        }

        addActivity("Market chart error", error.message);
    }
}


// ==================================================
// INFINITE HISTORICAL DATA
// ==================================================

async function loadOlderHistory() {
    if (
        historicalLoading ||
        !hasMoreHistory ||
        !oldestBarTime ||
        !selectedSymbol ||
        !candleSeries
    ) {
        return;
    }

    historicalLoading = true;

    try {
        const previousRange =
            marketChart.timeScale().getVisibleLogicalRange();

        const oldLength = currentBars.length;

        const params = new URLSearchParams({
            symbol: selectedSymbol,
            resolution: selectedResolution,
            bars: "500",
            to: String(oldestBarTime - 1)
        });

        const response = await fetch(
            `${API_BASE}/api/market/history?${params.toString()}`
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                getErrorMessage(data.detail, "Older history failed")
            );
        }

        const olderBars = (data.bars || []).filter(
            bar => bar.time < oldestBarTime
        );

        if (!olderBars.length) {
            hasMoreHistory = false;
            return;
        }

        const merged = [
            ...olderBars,
            ...currentBars
        ];

        // Remove any duplicate timestamps.
        const unique = new Map();

        merged.forEach(bar => {
            unique.set(bar.time, bar);
        });

        currentBars = Array.from(unique.values())
            .sort((a, b) => a.time - b.time);

        candleSeries.setData(currentBars);

        oldestBarTime = currentBars[0].time;

        if (olderBars.length < 500) {
            hasMoreHistory = false;
        }

        // Preserve the user's current visible position.
        if (previousRange) {
            const added = currentBars.length - oldLength;

            marketChart.timeScale().setVisibleLogicalRange({
                from: previousRange.from + added,
                to: previousRange.to + added
            });
        }

        if ($("market-updated")) {
            $("market-updated").textContent =
                `${currentBars.length} candles loaded`;
        }

    } catch (error) {
        console.error("Older history error:", error);

    } finally {
        historicalLoading = false;
    }
}


function handleVisibleRangeChange(range) {
    if (!range || historicalLoading || !hasMoreHistory) {
        return;
    }

    if (range.from < 15) {
        loadOlderHistory();
    }
}


// ==================================================
// LIVE WEBSOCKET CONNECTION
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

    closeLiveStream();

    const protocol =
        window.location.protocol === "https:"
            ? "wss:"
            : "ws:";

    const url =
        `${protocol}//${window.location.host}/ws/market` +
        `?symbol=${encodeURIComponent(selectedSymbol)}`;

    liveSocket = new WebSocket(url);

    setMarketStatus("Connecting live feed...");

    liveSocket.onopen = () => {
        setMarketStatus("Live stream connected");

        addActivity(
            "Live stream connected",
            `${selectedSymbol} · Pyth WebSocket`
        );
    };

    liveSocket.onmessage = (event) => {
        let message;

        try {
            message = JSON.parse(event.data);
        } catch {
            return;
        }

        if (message.type === "connection") {
            if (message.status === "connected") {
                setMarketStatus("Live · Pyth connected");
            }

            if (message.status === "reconnecting") {
                setMarketStatus("Reconnecting...");
            }

            return;
        }

        if (message.type === "heartbeat") {
            return;
        }

        if (message.type === "error") {
            setMarketStatus("Stream error");

            if ($("chart-message")) {
                $("chart-message").textContent = message.message;
            }

            return;
        }

        if (message.type === "price") {
            updateLiveCandle(message);
        }
    };

    liveSocket.onerror = () => {
        setMarketStatus("Live connection issue");
    };

    liveSocket.onclose = () => {
        setMarketStatus("Live feed disconnected");

        if (document.visibilityState !== "hidden") {
            reconnectTimer = setTimeout(() => {
                connectLiveStream();
            }, 4000);
        }
    };
}


// ==================================================
// LIVE CANDLE UPDATE
// ==================================================

function updateLiveCandle(message) {
    if (!candleSeries || !currentBars.length) return;

    const mantissa = Number(message.price);
    const exponent = Number(message.exponent);

    if (!Number.isFinite(mantissa) || !Number.isFinite(exponent)) {
        return;
    }

    const price = mantissa * Math.pow(10, exponent);

    if (!Number.isFinite(price) || price <= 0) {
        return;
    }

    const timestampUs = Number(
        message.feedUpdateTimestamp || message.timestampUs
    );

    if (!Number.isFinite(timestampUs) || timestampUs <= 0) {
        return;
    }

    const timestampSeconds = Math.floor(timestampUs / 1000000);

    const secondsPerCandle =
        Number(selectedResolution) * 60;

    const candleTime =
        Math.floor(timestampSeconds / secondsPerCandle)
        * secondsPerCandle;

    const latest = currentBars[currentBars.length - 1];

    let nextCandle;

    if (candleTime < latest.time) {
        // Ignore an older or delayed update.
        return;
    }

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
    }

    // Update only the latest candle. Do not reset the chart.
    candleSeries.update(nextCandle);

    if ($("market-price")) {
        $("market-price").textContent = price.toFixed(2);
    }

    if ($("market-status")) {
        $("market-status").textContent = "Live · Pyth";
    }

    if ($("market-updated")) {
        $("market-updated").textContent =
            `Live: ${new Date(timestampSeconds * 1000).toLocaleTimeString()}`;
    }

    if ($("last-updated")) {
        $("last-updated").textContent =
            new Date().toLocaleTimeString();
    }
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

            currentBars = [];
            oldestBarTime = null;
            hasMoreHistory = true;

            await loadMarketHistory(true);
        });
    }

    document.querySelectorAll(".timeframe-btn").forEach((button) => {
        button.addEventListener("click", async () => {
            document.querySelectorAll(".timeframe-btn").forEach((item) => {
                item.classList.remove("active");
            });

            button.classList.add("active");

            selectedResolution = button.dataset.resolution;

            currentBars = [];
            oldestBarTime = null;
            hasMoreHistory = true;

            // Keep the live connection; only reload the timeframe.
            await loadMarketHistory(false);
        });
    });
}


// ==================================================
// DASHBOARD REFRESH
// ==================================================

async function refreshDashboard() {
    await Promise.all([
        checkHealth(),
        loadDatabaseSummary()
    ]);

    if ($("last-updated")) {
        $("last-updated").textContent =
            new Date().toLocaleTimeString();
    }

    // Do not reload historical candles here.
    // The live WebSocket updates the chart continuously.
}


function startDashboardRefresh() {
    if (marketRefreshTimer) {
        clearInterval(marketRefreshTimer);
    }

    // Only refresh health and database status.
    marketRefreshTimer = setInterval(() => {
        refreshDashboard();
    }, 30000);
}


// ==================================================
// PAGE VISIBILITY
// ==================================================

document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
        if (!liveSocket || liveSocket.readyState === WebSocket.CLOSED) {
            connectLiveStream();
        }
    }
});


// ==================================================
// INITIALIZATION
// ==================================================

async function initializeDashboard() {
    updateClock();

    setInterval(updateClock, 1000);

    setupMarketControls();

    await checkHealth();
    await loadDatabaseSummary();
    await loadGoldSymbols();

    if ($("last-updated")) {
        $("last-updated").textContent =
            new Date().toLocaleTimeString();
    }

    const refreshButton = $("refresh-button");

    if (refreshButton) {
        refreshButton.addEventListener(
            "click",
            refreshDashboard
        );
    }

    startDashboardRefresh();
}


document.addEventListener(
    "DOMContentLoaded",
    initializeDashboard
);
