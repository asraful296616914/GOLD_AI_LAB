const API = window.location.hostname === "localhost"
    ? "http://localhost:8000"
    : "https://goldailab-production.up.railway.app";

function setText(id, value) {
    document.getElementById(id).textContent = value;
}

function log(message) {
    const box = document.getElementById("systemLog");
    const time = new Date().toLocaleTimeString();
    box.textContent = `${time} — ${message}`;
}

async function loadHealth() {
    try {
        const response = await fetch(`${API}/health`);
        if (!response.ok) throw new Error("Health check failed");

        const data = await response.json();

        setText("apiStatus", "ONLINE");
        setText(
            "dbStatus",
            data.database === "connected" ? "CONNECTED" : "OFFLINE"
        );

        log("Backend and database health checked.");
    } catch (error) {
        setText("apiStatus", "OFFLINE");
        setText("dbStatus", "UNKNOWN");
        log("Unable to connect to backend.");
    }
}

async function loadDatabase() {
    try {
        const response = await fetch(`${API}/database-summary`);
        if (!response.ok) throw new Error("Database unavailable");

        const data = await response.json();
        const tables = data.tables;

        const list = document.getElementById("tableList");
        list.innerHTML = "";

        let total = 0;

        Object.entries(tables).forEach(([name, count]) => {
            total += count;

            const row = document.createElement("div");
            row.className = "table-row";

            const title = document.createElement("span");
            title.textContent = name;

            const value = document.createElement("span");
            value.textContent = count.toLocaleString();

            row.append(title, value);
            list.appendChild(row);
        });

        setText("tableCount", Object.keys(tables).length);
        setText("totalRecords", total.toLocaleString());

        log("Database statistics loaded.");
    } catch (error) {
        document.getElementById("tableList").textContent =
            "Database statistics unavailable.";

        setText("totalRecords", "--");
        log("Database summary unavailable.");
    }
}

function updateClock() {
    setText("clock", new Date().toLocaleTimeString());
}

async function refreshDashboard() {
    await loadHealth();
    await loadDatabase();
}

updateClock();
refreshDashboard();

setInterval(updateClock, 1000);
setInterval(refreshDashboard, 30000);
