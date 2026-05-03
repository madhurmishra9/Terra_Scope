import { useState, useEffect, useRef, useCallback } from "react";

// ── API client ────────────────────────────────────────────────────────────────
const API = "http://localhost:8000/api";

async function apiGet(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`API ${path} → ${r.status}`);
  return r.json();
}

async function apiPost(path, body) {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`API ${path} → ${r.status}`);
  return r.json();
}

// ── Constants ─────────────────────────────────────────────────────────────────
const GCP_COLORS = {
  bigquery: "#4285F4",
  storage: "#0F9D58",
  dataflow: "#F4B400",
  pubsub: "#DB4437",
  dataproc: "#AA00FF",
  composer: "#00ACC1",
  spanner: "#FF6D00",
  bigtable: "#0288D1",
  default: "#5F6368",
};

const QUERY_TYPE_COLORS = {
  general: "#8B949E",
  issue: "#F85149",
  comparison: "#58A6FF",
  variable: "#3FB950",
  resource: "#D2A8FF",
  security: "#FFA657",
  dependency: "#79C0FF",
  unknown: "#484F58",
};

// ── Sub-components ────────────────────────────────────────────────────────────

function StatusDot({ status }) {
  const colors = {
    ready: "#3FB950",
    indexing: "#FFA657",
    not_indexed: "#484F58",
    idle: "#484F58",
    done: "#3FB950",
    unreachable: "#F85149",
  };
  const color = colors[status] || "#484F58";
  const pulse = status === "indexing";
  return (
    <span style={{
      display: "inline-block",
      width: 7, height: 7, borderRadius: "50%",
      background: color,
      boxShadow: pulse ? `0 0 0 2px ${color}44` : "none",
      animation: pulse ? "pulse 1.5s ease-in-out infinite" : "none",
      flexShrink: 0,
    }} />
  );
}

function Tag({ tag, selected, indexed, onClick }) {
  return (
    <button onClick={onClick} style={{
      display: "flex", alignItems: "center", gap: 6,
      width: "100%", background: "none", border: "none", cursor: "pointer",
      padding: "5px 10px", borderRadius: 5, textAlign: "left",
      background: selected ? "#1C2936" : "transparent",
      borderLeft: selected ? "2px solid #58A6FF" : "2px solid transparent",
      transition: "all 0.12s",
    }}>
      <StatusDot status={indexed ? "ready" : "not_indexed"} />
      <span style={{
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        fontSize: 11.5,
        color: selected ? "#E6EDF3" : "#8B949E",
      }}>{tag}</span>
    </button>
  );
}

function RepoCard({ repo, selected, onSelect }) {
  const color = GCP_COLORS[repo.gcp_product] || GCP_COLORS.default;
  const latestIndexed = repo.indexed_tags?.includes(repo.latest_tag);
  return (
    <button onClick={() => onSelect(repo)} style={{
      width: "100%", background: "none", border: "none", cursor: "pointer",
      padding: "10px 12px",
      borderBottom: "1px solid #21262D",
      borderLeft: `3px solid ${selected ? color : "transparent"}`,
      background: selected ? "#0D1117" : "transparent",
      textAlign: "left", transition: "all 0.1s",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div style={{
          width: 8, height: 8, borderRadius: 2,
          background: color, flexShrink: 0,
        }} />
        <span style={{
          fontSize: 12.5, fontWeight: 600,
          color: selected ? "#E6EDF3" : "#C9D1D9",
        }}>{repo.display_name}</span>
        <StatusDot status={latestIndexed ? "ready" : "not_indexed"} />
      </div>
      <div style={{
        fontSize: 10.5, color: "#484F58", marginTop: 3, paddingLeft: 16,
        fontFamily: "monospace",
      }}>{repo.name}</div>
    </button>
  );
}

function SourceChip({ source }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div style={{
      background: "#0D1117", border: "1px solid #21262D",
      borderRadius: 6, overflow: "hidden", marginBottom: 6,
    }}>
      <button onClick={() => setExpanded(!expanded)} style={{
        width: "100%", background: "none", border: "none", cursor: "pointer",
        padding: "6px 10px", display: "flex", alignItems: "center", gap: 8,
        textAlign: "left",
      }}>
        <span style={{ fontSize: 11, color: "#484F58" }}>📄</span>
        <span style={{
          fontFamily: "monospace", fontSize: 11, color: "#58A6FF", flex: 1,
        }}>{source.file_path}</span>
        <span style={{ fontSize: 10, color: "#484F58" }}>
          L{source.line_start}–{source.line_end}
        </span>
        <span style={{
          fontSize: 10, color: "#3FB950",
          background: "#3FB95011", padding: "1px 5px", borderRadius: 3,
        }}>{Math.round(source.relevance * 100)}%</span>
        <span style={{ color: "#484F58", fontSize: 12 }}>{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && (
        <div style={{
          borderTop: "1px solid #21262D",
          padding: "8px 10px",
        }}>
          <pre style={{
            margin: 0, fontSize: 11, lineHeight: 1.6,
            color: "#C9D1D9", whiteSpace: "pre-wrap", wordBreak: "break-word",
            fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
          }}>{source.snippet}</pre>
        </div>
      )}
    </div>
  );
}

function IssueCard({ solution }) {
  const [open, setOpen] = useState(true);
  return (
    <div style={{
      border: "1px solid #F8514944", borderRadius: 8,
      overflow: "hidden", marginTop: 12,
    }}>
      <div style={{
        background: "#F8514911", padding: "10px 14px",
        display: "flex", alignItems: "center", gap: 8,
        borderBottom: open ? "1px solid #F8514933" : "none",
        cursor: "pointer",
      }} onClick={() => setOpen(!open)}>
        <span>🔴</span>
        <span style={{ color: "#F85149", fontWeight: 700, fontSize: 12.5 }}>
          Issue Detected
        </span>
        <span style={{ marginLeft: "auto", color: "#484F58", fontSize: 12 }}>
          {open ? "▲" : "▼"}
        </span>
      </div>
      {open && (
        <div style={{ padding: "12px 14px" }}>
          <div style={{ fontSize: 12, color: "#8B949E", marginBottom: 10, lineHeight: 1.6 }}>
            <strong style={{ color: "#E6EDF3" }}>Root cause:</strong> {solution.root_cause}
          </div>
          <div style={{
            fontSize: 11, color: "#8B949E", marginBottom: 8,
            textTransform: "uppercase", letterSpacing: "0.07em",
          }}>Solution Steps</div>
          {solution.solution_steps.map((step, i) => (
            <div key={i} style={{
              display: "flex", gap: 10, marginBottom: 6, alignItems: "flex-start",
            }}>
              <span style={{
                width: 18, height: 18, borderRadius: "50%",
                background: "#3FB95022", border: "1px solid #3FB950",
                display: "flex", alignItems: "center", justifyContent: "center",
                color: "#3FB950", fontSize: 10, fontWeight: 700, flexShrink: 0, marginTop: 1,
              }}>{i + 1}</span>
              <span style={{ fontSize: 12, color: "#C9D1D9", lineHeight: 1.5 }}>{step}</span>
            </div>
          ))}
          {solution.gcloud_commands?.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{
                fontSize: 11, color: "#8B949E", marginBottom: 6,
                textTransform: "uppercase", letterSpacing: "0.07em",
              }}>gcloud Commands</div>
              {solution.gcloud_commands.map((cmd, i) => (
                <div key={i} style={{
                  background: "#0D1117", border: "1px solid #21262D", borderRadius: 5,
                  padding: "6px 10px", marginBottom: 4, fontFamily: "monospace",
                  fontSize: 11, color: "#79C0FF",
                }}>$ {cmd}</div>
              ))}
            </div>
          )}
          {solution.terraform_fix && (
            <div style={{ marginTop: 12 }}>
              <div style={{
                fontSize: 11, color: "#8B949E", marginBottom: 6,
                textTransform: "uppercase", letterSpacing: "0.07em",
              }}>Terraform Fix</div>
              <pre style={{
                background: "#0D1117", border: "1px solid #21262D", borderRadius: 5,
                padding: "8px 10px", margin: 0, fontSize: 11,
                color: "#D2A8FF", fontFamily: "monospace",
                whiteSpace: "pre-wrap", overflowX: "auto",
              }}>{solution.terraform_fix}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ConfidenceMeter({ value }) {
  const color = value >= 0.8 ? "#3FB950" : value >= 0.6 ? "#FFA657" : "#F85149";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div style={{
        width: 80, height: 4, background: "#21262D", borderRadius: 2, overflow: "hidden",
      }}>
        <div style={{
          width: `${value * 100}%`, height: "100%",
          background: color, borderRadius: 2, transition: "width 0.5s",
        }} />
      </div>
      <span style={{ fontSize: 11, color, fontFamily: "monospace" }}>
        {Math.round(value * 100)}%
      </span>
    </div>
  );
}

function Message({ msg }) {
  if (msg.role === "user") {
    return (
      <div style={{
        display: "flex", justifyContent: "flex-end", marginBottom: 16,
      }}>
        <div style={{
          background: "#1F6FEB22", border: "1px solid #1F6FEB44",
          borderRadius: "12px 12px 2px 12px",
          padding: "10px 14px", maxWidth: "75%",
        }}>
          <div style={{ fontSize: 13, color: "#E6EDF3", lineHeight: 1.6 }}>
            {msg.content}
          </div>
          {msg.meta && (
            <div style={{ fontSize: 10, color: "#484F58", marginTop: 4, fontFamily: "monospace" }}>
              {msg.meta}
            </div>
          )}
        </div>
      </div>
    );
  }

  const resp = msg.response;
  if (!resp) {
    return (
      <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
        <div style={{
          width: 28, height: 28, borderRadius: 6,
          background: "linear-gradient(135deg, #1F6FEB, #3FB950)",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 13, flexShrink: 0,
        }}>🔭</div>
        <div style={{
          background: "#161B22", border: "1px solid #21262D",
          borderRadius: "2px 12px 12px 12px",
          padding: "10px 14px", flex: 1,
        }}>
          {msg.loading ? (
            <div style={{ display: "flex", gap: 5, alignItems: "center" }}>
              {[0, 1, 2].map(i => (
                <div key={i} style={{
                  width: 6, height: 6, borderRadius: "50%", background: "#8B949E",
                  animation: `blink 1.2s ${i * 0.2}s ease-in-out infinite`,
                }} />
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 13, color: "#F85149" }}>{msg.error}</div>
          )}
        </div>
      </div>
    );
  }

  const qtColor = QUERY_TYPE_COLORS[resp.query_type] || "#8B949E";

  return (
    <div style={{ display: "flex", gap: 10, marginBottom: 20 }}>
      <div style={{
        width: 28, height: 28, borderRadius: 6,
        background: "linear-gradient(135deg, #1F6FEB, #3FB950)",
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 13, flexShrink: 0, marginTop: 1,
      }}>🔭</div>
      <div style={{ flex: 1 }}>
        {/* Header row */}
        <div style={{
          display: "flex", alignItems: "center", gap: 8, marginBottom: 8,
        }}>
          <span style={{
            background: qtColor + "22", color: qtColor,
            border: `1px solid ${qtColor}44`,
            borderRadius: 4, padding: "1px 7px", fontSize: 10, fontWeight: 700,
            textTransform: "uppercase", letterSpacing: "0.06em",
          }}>{resp.query_type}</span>
          <ConfidenceMeter value={resp.confidence} />
          {resp.grounded && (
            <span style={{
              fontSize: 10, color: "#3FB950", background: "#3FB95011",
              padding: "1px 6px", borderRadius: 3,
            }}>✓ grounded</span>
          )}
        </div>

        {/* Answer */}
        <div style={{
          background: "#161B22", border: "1px solid #21262D",
          borderRadius: "2px 12px 12px 12px",
          padding: "12px 16px",
        }}>
          <div style={{
            fontSize: 13, color: "#E6EDF3", lineHeight: 1.75,
            whiteSpace: "pre-wrap",
          }}>{resp.answer}</div>

          {/* Disclaimer */}
          {resp.disclaimer && (
            <div style={{
              marginTop: 10, padding: "8px 10px",
              background: "#FFA65711", border: "1px solid #FFA65733", borderRadius: 5,
              fontSize: 11.5, color: "#FFA657",
            }}>⚠ {resp.disclaimer}</div>
          )}

          {/* Issue card */}
          {resp.issue_solution && (
            <IssueCard solution={resp.issue_solution} />
          )}

          {/* Variables */}
          {resp.variables?.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{
                fontSize: 11, color: "#8B949E", marginBottom: 8,
                textTransform: "uppercase", letterSpacing: "0.07em",
              }}>Variables ({resp.variables.length})</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {resp.variables.map((v, i) => (
                  <div key={i} style={{
                    background: "#0D1117", border: "1px solid #21262D",
                    borderRadius: 5, padding: "4px 8px",
                    display: "flex", alignItems: "center", gap: 6,
                  }}>
                    <span style={{ color: v.required ? "#F85149" : "#3FB950", fontSize: 10 }}>
                      {v.required ? "required" : "optional"}
                    </span>
                    <span style={{
                      fontFamily: "monospace", fontSize: 11, color: "#D2A8FF",
                    }}>{v.name}</span>
                    <span style={{ fontSize: 10, color: "#484F58" }}>{v.type}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Sources */}
          {resp.sources?.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{
                fontSize: 11, color: "#8B949E", marginBottom: 6,
                textTransform: "uppercase", letterSpacing: "0.07em",
              }}>Sources ({resp.sources.length})</div>
              {resp.sources.slice(0, 4).map((src, i) => (
                <SourceChip key={i} source={src} />
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div style={{
          display: "flex", gap: 12, marginTop: 6, paddingLeft: 4,
        }}>
          {resp.repo_name && (
            <span style={{ fontSize: 10, color: "#484F58", fontFamily: "monospace" }}>
              {resp.repo_name}
            </span>
          )}
          {resp.tags_analyzed?.length > 0 && (
            <span style={{ fontSize: 10, color: "#484F58", fontFamily: "monospace" }}>
              @ {resp.tags_analyzed.join(", ")}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────

export default function TerraScope() {
  const [repos, setRepos] = useState([]);
  const [selectedRepo, setSelectedRepo] = useState(null);
  const [selectedTag, setSelectedTag] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [health, setHealth] = useState(null);
  const [indexing, setIndexing] = useState(false);
  const [sidebarTab, setSidebarTab] = useState("repos"); // repos | tags
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  // Load repos on mount
  useEffect(() => {
    apiGet("/repos").then(data => {
      setRepos(data);
      if (data.length > 0) {
        setSelectedRepo(data[0]);
        if (data[0].latest_tag) setSelectedTag(data[0].latest_tag);
      }
    }).catch(() => {});

    apiGet("/health").then(setHealth).catch(() => {});
  }, []);

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = useCallback(async () => {
    if (!input.trim() || loading) return;
    const question = input.trim();
    setInput("");

    const userMsg = {
      id: Date.now(),
      role: "user",
      content: question,
      meta: selectedRepo
        ? `${selectedRepo.name} @ ${selectedTag || "latest"}`
        : "All repos",
    };
    const agentMsg = {
      id: Date.now() + 1,
      role: "agent",
      loading: true,
      response: null,
      error: null,
    };

    setMessages(prev => [...prev, userMsg, agentMsg]);
    setLoading(true);

    try {
      const resp = await apiPost("/query", {
        question,
        repo_name: selectedRepo?.name || null,
        tag: selectedTag || null,
        strict_mode: true,
      });
      setMessages(prev => prev.map(m =>
        m.id === agentMsg.id
          ? { ...m, loading: false, response: resp }
          : m
      ));
    } catch (e) {
      setMessages(prev => prev.map(m =>
        m.id === agentMsg.id
          ? { ...m, loading: false, error: `Error: ${e.message}. Is TerraScope running?` }
          : m
      ));
    } finally {
      setLoading(false);
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [input, loading, selectedRepo, selectedTag]);

  const handleIndex = async () => {
    setIndexing(true);
    try {
      await apiPost("/index", { repo_name: selectedRepo?.name || null, force: false });
      setTimeout(async () => {
        const data = await apiGet("/repos");
        setRepos(data);
        setIndexing(false);
      }, 2000);
    } catch (e) {
      setIndexing(false);
    }
  };

  const ollamaOk = health?.ollama === "running";

  return (
    <div style={{
      display: "flex", flexDirection: "column", height: "100vh",
      background: "#0D1117", color: "#C9D1D9",
      fontFamily: "'JetBrains Mono', 'Fira Code', 'SF Mono', monospace",
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #0D1117; }
        ::-webkit-scrollbar-thumb { background: #21262D; border-radius: 3px; }
        @keyframes blink {
          0%, 80%, 100% { opacity: 0.2; transform: scale(0.8); }
          40% { opacity: 1; transform: scale(1); }
        }
        @keyframes pulse {
          0%, 100% { box-shadow: 0 0 0 0 currentColor; }
          50% { box-shadow: 0 0 0 4px transparent; }
        }
        textarea:focus { outline: none; }
        button:hover { opacity: 0.85; }
      `}</style>

      {/* ── Top bar ── */}
      <div style={{
        height: 48, borderBottom: "1px solid #21262D",
        display: "flex", alignItems: "center", padding: "0 16px", gap: 16,
        background: "#161B22", flexShrink: 0,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{
            width: 24, height: 24, borderRadius: 5,
            background: "linear-gradient(135deg, #1F6FEB, #3FB950)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 12,
          }}>🔭</div>
          <span style={{ fontWeight: 700, fontSize: 14, color: "#E6EDF3" }}>
            TerraScope
          </span>
          <span style={{
            fontSize: 10, color: "#484F58", padding: "1px 5px",
            background: "#21262D", borderRadius: 3,
          }}>v1.0</span>
        </div>

        <div style={{ flex: 1 }} />

        {/* Ollama status */}
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <StatusDot status={ollamaOk ? "ready" : "unreachable"} />
          <span style={{ fontSize: 11, color: "#8B949E" }}>
            {health ? (ollamaOk ? health.model : "Ollama offline") : "Checking..."}
          </span>
        </div>

        {/* Grounding badge */}
        <div style={{
          fontSize: 10, color: "#3FB950", background: "#3FB95011",
          border: "1px solid #3FB95033", padding: "2px 8px", borderRadius: 3,
        }}>
          STRICT GROUNDING
        </div>

        {/* Index button */}
        <button onClick={handleIndex} disabled={indexing} style={{
          background: indexing ? "#21262D" : "#1F6FEB22",
          border: "1px solid #1F6FEB44", borderRadius: 5,
          color: "#58A6FF", fontSize: 11, padding: "5px 12px", cursor: "pointer",
          display: "flex", alignItems: "center", gap: 6,
        }}>
          {indexing ? (
            <><StatusDot status="indexing" /> Indexing...</>
          ) : (
            <><span>⟳</span> Index Repos</>
          )}
        </button>
      </div>

      {/* ── Body ── */}
      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>

        {/* ── Sidebar ── */}
        <div style={{
          width: 220, borderRight: "1px solid #21262D",
          display: "flex", flexDirection: "column",
          background: "#161B22", flexShrink: 0,
        }}>
          {/* Sidebar tabs */}
          <div style={{
            display: "flex", borderBottom: "1px solid #21262D",
          }}>
            {["repos", "tags"].map(tab => (
              <button key={tab} onClick={() => setSidebarTab(tab)} style={{
                flex: 1, background: "none", border: "none", cursor: "pointer",
                padding: "8px 0", fontSize: 10.5, fontFamily: "inherit",
                textTransform: "uppercase", letterSpacing: "0.06em",
                color: sidebarTab === tab ? "#58A6FF" : "#484F58",
                borderBottom: `2px solid ${sidebarTab === tab ? "#58A6FF" : "transparent"}`,
              }}>{tab}</button>
            ))}
          </div>

          <div style={{ flex: 1, overflowY: "auto" }}>
            {sidebarTab === "repos" && (
              <>
                {repos.length === 0 ? (
                  <div style={{ padding: 12, fontSize: 11, color: "#484F58", lineHeight: 1.6 }}>
                    No repos configured.<br />
                    Edit terrascope.config.yaml to add repos.
                  </div>
                ) : repos.map(repo => (
                  <RepoCard
                    key={repo.name}
                    repo={repo}
                    selected={selectedRepo?.name === repo.name}
                    onSelect={r => {
                      setSelectedRepo(r);
                      setSelectedTag(r.latest_tag);
                      setSidebarTab("tags");
                    }}
                  />
                ))}
              </>
            )}

            {sidebarTab === "tags" && selectedRepo && (
              <>
                <div style={{
                  padding: "8px 12px",
                  fontSize: 10, color: "#484F58", textTransform: "uppercase",
                  letterSpacing: "0.06em", borderBottom: "1px solid #21262D",
                }}>
                  {selectedRepo.display_name} — {selectedRepo.tags?.length || 0} tags
                </div>
                {(selectedRepo.tags || []).map(tag => (
                  <Tag
                    key={tag}
                    tag={tag}
                    selected={selectedTag === tag}
                    indexed={selectedRepo.indexed_tags?.includes(tag)}
                    onClick={() => setSelectedTag(tag)}
                  />
                ))}
              </>
            )}
          </div>

          {/* Repo footer */}
          {selectedRepo && (
            <div style={{
              borderTop: "1px solid #21262D", padding: "10px 12px",
            }}>
              <div style={{
                fontSize: 10, color: "#484F58", lineHeight: 1.6,
              }}>
                <div style={{ color: "#8B949E", marginBottom: 2 }}>{selectedRepo.display_name}</div>
                <div>{selectedRepo.indexed_tags?.length || 0}/{selectedRepo.tags?.length || 0} tags indexed</div>
              </div>
            </div>
          )}
        </div>

        {/* ── Chat area ── */}
        <div style={{
          flex: 1, display: "flex", flexDirection: "column", overflow: "hidden",
        }}>
          {/* Context bar */}
          <div style={{
            height: 36, borderBottom: "1px solid #21262D",
            display: "flex", alignItems: "center", padding: "0 16px", gap: 10,
            background: "#0D1117", flexShrink: 0,
          }}>
            {selectedRepo ? (
              <>
                <span style={{
                  width: 8, height: 8, borderRadius: 2, flexShrink: 0,
                  background: GCP_COLORS[selectedRepo.gcp_product] || GCP_COLORS.default,
                }} />
                <span style={{ fontSize: 11.5, color: "#C9D1D9" }}>
                  {selectedRepo.display_name}
                </span>
                {selectedTag && (
                  <>
                    <span style={{ color: "#21262D" }}>›</span>
                    <span style={{
                      fontFamily: "monospace", fontSize: 11, color: "#58A6FF",
                      background: "#1F6FEB11", padding: "1px 6px", borderRadius: 3,
                    }}>{selectedTag}</span>
                    <StatusDot status={
                      selectedRepo.indexed_tags?.includes(selectedTag) ? "ready" : "not_indexed"
                    } />
                  </>
                )}
              </>
            ) : (
              <span style={{ fontSize: 11, color: "#484F58" }}>Select a repo from the sidebar</span>
            )}
          </div>

          {/* Messages */}
          <div style={{
            flex: 1, overflowY: "auto", padding: "20px 24px",
          }}>
            {messages.length === 0 && (
              <div style={{
                display: "flex", flexDirection: "column", alignItems: "center",
                justifyContent: "center", height: "100%", gap: 16, opacity: 0.5,
              }}>
                <div style={{
                  width: 56, height: 56, borderRadius: 14,
                  background: "linear-gradient(135deg, #1F6FEB22, #3FB95022)",
                  border: "1px solid #1F6FEB33",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 28,
                }}>🔭</div>
                <div style={{ textAlign: "center" }}>
                  <div style={{ fontSize: 14, color: "#8B949E", marginBottom: 6 }}>
                    TerraScope ready
                  </div>
                  <div style={{ fontSize: 11.5, color: "#484F58", lineHeight: 1.7 }}>
                    Ask about any tag, variable, resource,<br />
                    issue, or change across your Terraform modules.
                  </div>
                </div>
                <div style={{
                  display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, maxWidth: 480,
                }}>
                  {[
                    "What GCP resources does this module create?",
                    "What variables are required?",
                    "What changed between v1.0 and v2.0?",
                    "Why does plan fail with 403 on BigQuery?",
                  ].map((q, i) => (
                    <button key={i} onClick={() => setInput(q)} style={{
                      background: "#161B22", border: "1px solid #21262D",
                      borderRadius: 6, padding: "8px 10px", cursor: "pointer",
                      fontSize: 11.5, color: "#8B949E", textAlign: "left",
                      fontFamily: "inherit", lineHeight: 1.4,
                    }}>{q}</button>
                  ))}
                </div>
              </div>
            )}

            {messages.map(msg => (
              <Message key={msg.id} msg={msg} />
            ))}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div style={{
            borderTop: "1px solid #21262D", padding: "12px 16px",
            background: "#0D1117", flexShrink: 0,
          }}>
            <div style={{
              display: "flex", gap: 10, alignItems: "flex-end",
              background: "#161B22", border: `1px solid ${loading ? "#1F6FEB44" : "#21262D"}`,
              borderRadius: 8, padding: "10px 14px",
              transition: "border-color 0.2s",
            }}>
              <textarea
                ref={inputRef}
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={e => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleSend();
                  }
                }}
                placeholder={
                  selectedRepo
                    ? `Ask about ${selectedRepo.display_name} @ ${selectedTag || "latest"}...`
                    : "Select a repo first..."
                }
                disabled={loading || !selectedRepo}
                rows={1}
                style={{
                  flex: 1, background: "none", border: "none",
                  color: "#E6EDF3", fontSize: 13, fontFamily: "inherit",
                  resize: "none", lineHeight: 1.5,
                  maxHeight: 120, overflowY: "auto",
                  opacity: !selectedRepo ? 0.4 : 1,
                }}
              />
              <button onClick={handleSend} disabled={loading || !input.trim() || !selectedRepo}
                style={{
                  width: 32, height: 32, borderRadius: 6,
                  background: loading ? "#21262D" : "#1F6FEB",
                  border: "none", cursor: "pointer",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 14, flexShrink: 0,
                  opacity: (!input.trim() || !selectedRepo) ? 0.4 : 1,
                  transition: "all 0.15s",
                }}>
                {loading ? "⋯" : "↑"}
              </button>
            </div>
            <div style={{
              display: "flex", justifyContent: "space-between",
              marginTop: 6, paddingLeft: 2,
            }}>
              <span style={{ fontSize: 10, color: "#484F58" }}>
                Enter to send · Shift+Enter for newline
              </span>
              <span style={{ fontSize: 10, color: "#484F58" }}>
                Strict mode · {ollamaOk ? "🟢 Ollama" : "🔴 Ollama offline"}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
