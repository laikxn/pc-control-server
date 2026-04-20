import React, { useEffect, useRef, useState } from "react";

export default function App() {
  const wsRef = useRef(null);

  const [devices, setDevices] = useState({});
  const [logs, setLogs] = useState([]);

  const SERVER_URL = "ws://192.168.1.230:8000";

  // -----------------------------
  const upsertDevice = (id, data) => {
    if (!id) return;

    setDevices((prev) => {
      const existing = prev[id] || {};

      return {
        ...prev,
        [id]: {
          id,
          status: data.status ?? existing.status ?? "unknown",
          lastSeen: data.lastSeen ?? existing.lastSeen ?? Date.now(),
        },
      };
    });
  };

  // -----------------------------
  // 🔥 LOG FORMATTER ADDED (FIX)
  const formatLog = (msg) => {
    if (!msg) return "unknown";

    // structured server logs
    if (msg.type === "server_log") {
      const event = msg.event || "EVENT";
      const dev = msg.data?.device_id ? ` (${msg.data.device_id})` : "";
      return `${event}${dev}`;
    }

    // pc status messages
    if (msg.type === "pc_status") {
      return `DEVICE ${msg.device_id} → ${msg.status}`;
    }

    // fallback
    return typeof msg === "string" ? msg : JSON.stringify(msg);
  };

  // -----------------------------
  const addLog = (msg) => {
    setLogs((prev) => {
      const next = [
        {
          time: new Date().toLocaleTimeString(),
          msg: formatLog(msg), // 🔥 CHANGED HERE ONLY
        },
        ...prev,
      ];

      return next.slice(0, 100); // keep last 100 logs
    });
  };

  // -----------------------------
  useEffect(() => {
    const ws = new WebSocket(SERVER_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      addLog("Connected to server");

      ws.send(
        JSON.stringify({
          type: "register_dashboard",
        })
      );
    };

    ws.onmessage = (event) => {
      let msg;

      try {
        msg = JSON.parse(event.data);
      } catch {
        addLog("Bad packet: " + event.data);
        return;
      }

      addLog(msg);

      const id = msg.device_id;

      // ---------------- DEVICE STATE ----------------
      if (msg.type === "pc_status") {
        upsertDevice(id, {
          status: msg.status,
          lastSeen: Date.now(),
        });
      }

      if (msg.type === "pc_activity") {
        upsertDevice(id, {
          status: "online",
          lastSeen: Date.now(),
        });
      }

      // ---------------- DEBUG LOGS ----------------
      if (msg.type === "debug") {
        addLog(msg.data);
      }
    };

    ws.onclose = () => addLog("Disconnected");
    ws.onerror = () => addLog("Connection error");

    return () => ws.close();
  }, []);

  // -----------------------------
  useEffect(() => {
    const interval = setInterval(() => {
      setDevices((prev) => {
        const now = Date.now();
        const updated = { ...prev };

        Object.values(updated).forEach((d) => {
          const diff = now - (d.lastSeen || 0);

          if (diff > 30000) d.status = "offline";
          else if (diff > 10000) d.status = "idle";
          else d.status = "online";
        });

        return { ...updated };
      });
    }, 1000);

    return () => clearInterval(interval);
  }, []);

  // -----------------------------
  const getColor = (status) => {
    if (status === "online") return "#4ade80";
    if (status === "idle") return "#fbbf24";
    return "#ef4444";
  };

  // -----------------------------
  return (
    <div style={styles.container}>
      <h1 style={{ color: "white" }}>PC Dashboard</h1>

      {/* DEVICES */}
      <h3 style={{ color: "white" }}>Devices</h3>

      {Object.keys(devices).length === 0 && (
        <p style={{ color: "#aaa" }}>Waiting for devices to connect...</p>
      )}

      {Object.values(devices).map((d) => (
        <div key={d.id} style={styles.card}>
          <h3 style={{ color: "white" }}>{d.id}</h3>

          <p style={{ color: getColor(d.status) }}>
            {d.status?.toUpperCase()}
          </p>

          <p style={{ color: "#aaa", fontSize: 12 }}>
            Last seen: {Math.floor((Date.now() - d.lastSeen) / 1000)}s ago
          </p>

          <div style={{ display: "flex", gap: 10 }}>
            <button
              onClick={() =>
                wsRef.current.send(
                  JSON.stringify({
                    type: "wake_pc",
                    device_id: d.id,
                  })
                )
              }
            >
              Wake
            </button>

            <button
              onClick={() =>
                wsRef.current.send(
                  JSON.stringify({
                    type: "shutdown_pc",
                    device_id: d.id,
                  })
                )
              }
            >
              Shutdown
            </button>

            <button
              onClick={() =>
                wsRef.current.send(
                  JSON.stringify({
                    type: "restart_pc",
                    device_id: d.id,
                  })
                )
              }
            >
              Restart
            </button>
          </div>
        </div>
      ))}

      {/* LOGS */}
      <h3 style={{ color: "white", marginTop: 20 }}>Live Logs</h3>

      <div style={styles.logBox}>
        {logs.map((l, i) => (
          <div key={i} style={styles.logLine}>
            <span style={{ color: "#666" }}>{l.time}</span>{" "}
            <span style={{ color: "#aaa" }}>{l.msg}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// -----------------------------
const styles = {
  container: {
    background: "#0b0f14",
    minHeight: "100vh",
    padding: 20,
  },
  card: {
    background: "#141a22",
    padding: 15,
    borderRadius: 10,
    marginTop: 10,
  },
  logBox: {
    background: "#0f141b",
    padding: 10,
    borderRadius: 10,
    height: 250,
    overflowY: "auto",
  },
  logLine: {
    fontSize: 12,
    marginBottom: 4,
  },
};