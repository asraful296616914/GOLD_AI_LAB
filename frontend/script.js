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

const $ = (id) => document.getElementById(id);


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


async function checkHealth() {
    try {
        const response = await fetch(`${API_BASE}/health`);
        const data = await response.json();

        if (!response.ok) {
            throw new Error("Backend health check failed");
        }

        $("api-status").textContent = "Online";
        $("database-status").textContent = data.database || "Unknown";
        $("sidebar-status").textContent = "Connected";

        $("sidebar-status-dot").style.background = "#35d69a";

    } catch (error) {
        $("api-status").textContent = "Offline";
        $("database-status").textContent = "Unknown";
        $("sidebar-status").textContent = "Disconnected";

        $("sidebar-status-dot").style.background = "#ff6b75";
    }
}


async function loadDatabaseSummary() {
    try {
        const response = await fetch(`${API_BASE}/database-summary`);

        if (!response.ok) {
            throw new Error("Database summary unavailable");
        }

        const data = await response.json();
        const tableBody = $("database-table-body");

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

        $("table-count").textContent = totalTables;
        $("db-connection-label").textContent = "Connected";

    } catch (error) {
        $("db-connection-label").textContent = "Unavailable";
    }
}


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
            secondsVisible: false
        },

        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal
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

    if (window.ResizeObserver) {
        const observer = new ResizeObserver(() => {
            if (marketChart) {
                marketChart.applyOptions({
                    width: container.clientWidth,
                    height: container.clientHeight
                });
            }
        });

        observer.observe(container);
    }
}


async function loadGoldSymbols() {
    const select = $("market-symbol");

    try {
        const response = await fetch(
            `${API_BASE}/api/market/symbols`
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "Could not load Gold symbols");
        }

        if (!data.symbols || data.symbols.length === 0) {
            throw new Error("No accessible Gold feed was found");
        }

        select.replaceChildren();

        data.symbols.forEach((item) => {
            if (!item.symbol) return;

            const option = document.createElement("option");
            option.value = item.symbol;
            option.textContent = item.name || item.symbol;

            select.appendChild(option);
        });

        if (!select.options.length) {
            throw new Error("No usable Gold symbol was returned");
        }

        selectedSymbol = select.value;

        await loadMarketHistory();

    } catch (error) {
        select.replaceChildren();

        const option = document.createElement("option");
        option.textContent = "Gold feed unavailable";
        option.value = "";

        select.appendChild(option);

        $("market-status").textContent = "Feed unavailable";
        $("chart-message").textContent = error.message;

        addActivity("Market data error", error.message);
    }
}


async function loadMarketHistory() {
    if (!selectedSymbol) return;

    $("market-status").textContent = "Updating...";
    $("chart-message").textContent = "Loading candle data...";

    try {
        const params = new URLSearchParams({
            symbol: selectedSymbol,
            resolution: selectedResolution,
            bars: "300"
        });

        const response = await fetch(
            `${API_BASE}/api/market/history?${params.toString()}`
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "Market history request failed");
        }

        if (!data.bars || data.bars.length === 0) {
            throw new Error("No candle data is available for this symbol");
        }

        if (!candleSeries) {
            initializeChart();
        }

        candleSeries.setData(data.bars);

        marketChart.timeScale().fitContent();

        const latest = data.bars[data.bars.length - 1];

        $("market-price").textContent =
            Number(latest.close).toFixed(2);

        $("market-status").textContent = "Pyth data connected";
        $("market-updated").textContent =
            `Updated: ${new Date().toLocaleTimeString()}`;

        $("market-source").textContent =
            `Source: Pyth · ${data.symbol}`;

        $("chart-message").textContent = "";

        addActivity(
            "Market chart updated",
            `${data.symbol} · ${data.count} candles received`
        );

    } catch (error) {
        $("market-status").textContent = "Data unavailable";
        $("chart-message").textContent = error.message;

        addActivity("Market chart error", error.message);
    }
}


function setupMarketControls() {
    $("market-symbol").addEventListener("change", async (event) => {
        selectedSymbol = event.target.value;
        await loadMarketHistory();
    });

    document.querySelectorAll(".timeframe-btn").forEach((button) => {
        button.addEventListener("click", async () => {
            document.querySelectorAll(".timeframe-btn").forEach((item) => {
                item.classList.remove("active");
            });

            button.classList.add("active");

            selectedResolution = button.dataset.resolution;

            await loadMarketHistory();
        });
    });
}


async function refreshDashboard() {
    await Promise.all([
        checkHealth(),
        loadDatabaseSummary()
    ]);

    $("last-updated").textContent =
        new Date().toLocaleTimeString();

    if (selectedSymbol) {
        await loadMarketHistory();
    }
}


function startAutoRefresh() {
    if (marketRefreshTimer) {
        clearInterval(marketRefreshTimer);
    }

    marketRefreshTimer = setInterval(() => {
        refreshDashboard();
    }, 30000);
}


async function initializeDashboard() {
    updateClock();
    setInterval(updateClock, 1000);

    setupMarketControls();

    await checkHealth();
    await loadDatabaseSummary();
    await loadGoldSymbols();

    $("last-updated").textContent =
        new Date().toLocaleTimeString();

    $("refresh-button").addEventListener("click", refreshDashboard);

    startAutoRefresh();
}


document.addEventListener("DOMContentLoaded", initializeDashboard);
