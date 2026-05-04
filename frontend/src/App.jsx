import { useState, useEffect, useRef, useCallback } from "react";

// ── API client ────────────────────────────────────────────────────────────────
const API     = "http://localhost:8000/api";
const GA_API  = "http://localhost:8000/api/ga";

async function apiGet(path, base = API) {
  const r = await fetch(`${base}${path}`);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}
async function apiPost(path, body, base = API) {
  const r = await fetch(`${base}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

// ── Constants ─────────────────────────────────────────────────────────────────
const GCP_COLORS = {
  bigquery:"#4285F4", storage:"#0F9D58", dataflow:"#F4B400",
  pubsub:"#DB4437", dataproc:"#AA00FF", composer:"#00ACC1",
  spanner:"#FF6D00", bigtable:"#0288D1", dataplex:"#E91E63",
  vertex_ai:"#9C27B0", datastream:"#00BCD4", default:"#5F6368",
};
const QUERY_TYPE_COLORS = {
  general:"#8B949E", issue:"#F85149", comparison:"#58A6FF",
  variable:"#3FB950", resource:"#D2A8FF", security:"#FFA657",
  dependency:"#79C0FF", unknown:"#484F58",
};
const STAGE_LABELS = {
  idle:"Idle", detecting_ga:"Detecting GA release",
  scanning_gcp_service:"Scanning GCP service",
  analyzing_changes:"Analysing changes", creating_branch:"Creating branch",
  implementing_changes:"Implementing changes", validating_code:"Validating code",
  checking_pr:"Checking PR", creating_pr:"Creating PR",
  updating_pr:"Updating PR", done:"Done", failed:"Failed",
};
const IMPACT_COLORS = {
  new_resource:"#3FB950", new_argument:"#58A6FF",
  api_only:"#FFA657", unknown:"#484F58",
};

// ── Shared micro-components ───────────────────────────────────────────────────
function StatusDot({ status }) {
  const colors = { ready:"#3FB950", indexing:"#FFA657", not_indexed:"#484F58",
    idle:"#484F58", done:"#3FB950", unreachable:"#F85149", running:"#FFA657" };
  const color = colors[status] || "#484F58";
  const pulse = status === "indexing" || status === "running";
  return (
    <span style={{
      display:"inline-block", width:7, height:7, borderRadius:"50%",
      background:color,
      boxShadow: pulse ? `0 0 0 2px ${color}44` : "none",
      animation: pulse ? "pulse 1.5s ease-in-out infinite" : "none",
      flexShrink:0,
    }} />
  );
}

function Badge({ label, color = "#484F58", size = 10 }) {
  return (
    <span style={{
      background: color + "22", color, border: `1px solid ${color}44`,
      borderRadius:3, padding:"1px 6px", fontSize:size, fontWeight:700,
      textTransform:"uppercase", letterSpacing:"0.05em", whiteSpace:"nowrap",
    }}>{label}</span>
  );
}

function Tag({ tag, selected, indexed, onClick }) {
  return (
    <button onClick={onClick} style={{
      display:"flex", alignItems:"center", gap:6, width:"100%",
      background: selected ? "#1C2936" : "transparent",
      border:"none", cursor:"pointer", padding:"5px 10px", borderRadius:5,
      borderLeft: selected ? "2px solid #58A6FF" : "2px solid transparent",
      transition:"all 0.12s",
    }}>
      <StatusDot status={indexed ? "ready" : "not_indexed"} />
      <span style={{ fontFamily:"monospace", fontSize:11.5,
        color: selected ? "#E6EDF3" : "#8B949E" }}>{tag}</span>
    </button>
  );
}

function RepoCard({ repo, selected, onSelect }) {
  const color = GCP_COLORS[repo.gcp_product] || GCP_COLORS.default;
  const latestIndexed = repo.indexed_tags?.includes(repo.latest_tag);
  return (
    <button onClick={() => onSelect(repo)} style={{
      width:"100%", background: selected ? "#0D1117" : "transparent",
      border:"none", cursor:"pointer", padding:"10px 12px",
      borderBottom:"1px solid #21262D",
      borderLeft:`3px solid ${selected ? color : "transparent"}`,
      textAlign:"left", transition:"all 0.1s",
    }}>
      <div style={{ display:"flex", alignItems:"center", gap:8 }}>
        <div style={{ width:8, height:8, borderRadius:2, background:color, flexShrink:0 }} />
        <span style={{ fontSize:12.5, fontWeight:600, color: selected ? "#E6EDF3" : "#C9D1D9" }}>
          {repo.display_name}
        </span>
        <StatusDot status={latestIndexed ? "ready" : "not_indexed"} />
      </div>
      <div style={{ fontSize:10.5, color:"#484F58", marginTop:3, paddingLeft:16, fontFamily:"monospace" }}>
        {repo.name}
      </div>
    </button>
  );
}

function SourceChip({ source }) {
  const [exp, setExp] = useState(false);
  return (
    <div style={{ background:"#0D1117", border:"1px solid #21262D", borderRadius:6, overflow:"hidden", marginBottom:6 }}>
      <button onClick={() => setExp(!exp)} style={{
        width:"100%", background:"none", border:"none", cursor:"pointer",
        padding:"6px 10px", display:"flex", alignItems:"center", gap:8, textAlign:"left",
      }}>
        <span style={{ fontFamily:"monospace", fontSize:11, color:"#58A6FF", flex:1 }}>{source.file_path}</span>
        <span style={{ fontSize:10, color:"#484F58" }}>L{source.line_start}–{source.line_end}</span>
        <span style={{ fontSize:10, color:"#3FB950", background:"#3FB95011", padding:"1px 5px", borderRadius:3 }}>
          {Math.round(source.relevance * 100)}%
        </span>
        <span style={{ color:"#484F58", fontSize:12 }}>{exp ? "▲" : "▼"}</span>
      </button>
      {exp && (
        <div style={{ borderTop:"1px solid #21262D", padding:"8px 10px" }}>
          <pre style={{ margin:0, fontSize:11, lineHeight:1.6, color:"#C9D1D9",
            whiteSpace:"pre-wrap", wordBreak:"break-word", fontFamily:"monospace" }}>
            {source.snippet}
          </pre>
        </div>
      )}
    </div>
  );
}

function IssueCard({ solution }) {
  const [open, setOpen] = useState(true);
  return (
    <div style={{ border:"1px solid #F8514944", borderRadius:8, overflow:"hidden", marginTop:12 }}>
      <div onClick={() => setOpen(!open)} style={{
        background:"#F8514911", padding:"10px 14px", display:"flex", alignItems:"center", gap:8,
        borderBottom: open ? "1px solid #F8514933" : "none", cursor:"pointer",
      }}>
        <span>🔴</span>
        <span style={{ color:"#F85149", fontWeight:700, fontSize:12.5 }}>Issue Detected</span>
        <span style={{ marginLeft:"auto", color:"#484F58", fontSize:12 }}>{open ? "▲" : "▼"}</span>
      </div>
      {open && (
        <div style={{ padding:"12px 14px" }}>
          <div style={{ fontSize:12, color:"#8B949E", marginBottom:10, lineHeight:1.6 }}>
            <strong style={{ color:"#E6EDF3" }}>Root cause:</strong> {solution.root_cause}
          </div>
          <div style={{ fontSize:11, color:"#8B949E", marginBottom:8, textTransform:"uppercase", letterSpacing:"0.07em" }}>
            Solution Steps
          </div>
          {solution.solution_steps?.map((step, i) => (
            <div key={i} style={{ display:"flex", gap:10, marginBottom:6, alignItems:"flex-start" }}>
              <span style={{ width:18, height:18, borderRadius:"50%", background:"#3FB95022",
                border:"1px solid #3FB950", display:"flex", alignItems:"center", justifyContent:"center",
                color:"#3FB950", fontSize:10, fontWeight:700, flexShrink:0, marginTop:1 }}>{i+1}</span>
              <span style={{ fontSize:12, color:"#C9D1D9", lineHeight:1.5 }}>{step}</span>
            </div>
          ))}
          {solution.gcloud_commands?.length > 0 && (
            <div style={{ marginTop:12 }}>
              <div style={{ fontSize:11, color:"#8B949E", marginBottom:6, textTransform:"uppercase", letterSpacing:"0.07em" }}>
                gcloud Commands
              </div>
              {solution.gcloud_commands.map((cmd, i) => (
                <div key={i} style={{ background:"#0D1117", border:"1px solid #21262D", borderRadius:5,
                  padding:"6px 10px", marginBottom:4, fontFamily:"monospace", fontSize:11, color:"#79C0FF" }}>
                  $ {cmd}
                </div>
              ))}
            </div>
          )}
          {solution.terraform_fix && (
            <div style={{ marginTop:12 }}>
              <div style={{ fontSize:11, color:"#8B949E", marginBottom:6, textTransform:"uppercase", letterSpacing:"0.07em" }}>
                Terraform Fix
              </div>
              <pre style={{ background:"#0D1117", border:"1px solid #21262D", borderRadius:5, padding:"8px 10px",
                margin:0, fontSize:11, color:"#D2A8FF", fontFamily:"monospace", whiteSpace:"pre-wrap" }}>
                {solution.terraform_fix}
              </pre>
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
    <div style={{ display:"flex", alignItems:"center", gap:8 }}>
      <div style={{ width:80, height:4, background:"#21262D", borderRadius:2, overflow:"hidden" }}>
        <div style={{ width:`${value*100}%`, height:"100%", background:color, borderRadius:2 }} />
      </div>
      <span style={{ fontSize:11, color, fontFamily:"monospace" }}>{Math.round(value*100)}%</span>
    </div>
  );
}

function Message({ msg }) {
  if (msg.role === "user") {
    return (
      <div style={{ display:"flex", justifyContent:"flex-end", marginBottom:16 }}>
        <div style={{ background:"#1F6FEB22", border:"1px solid #1F6FEB44",
          borderRadius:"12px 12px 2px 12px", padding:"10px 14px", maxWidth:"75%" }}>
          <div style={{ fontSize:13, color:"#E6EDF3", lineHeight:1.6 }}>{msg.content}</div>
          {msg.meta && <div style={{ fontSize:10, color:"#484F58", marginTop:4, fontFamily:"monospace" }}>{msg.meta}</div>}
        </div>
      </div>
    );
  }
  const resp = msg.response;
  if (!resp) {
    return (
      <div style={{ display:"flex", gap:10, marginBottom:16 }}>
        <div style={{ width:28, height:28, borderRadius:6, background:"linear-gradient(135deg,#1F6FEB,#3FB950)",
          display:"flex", alignItems:"center", justifyContent:"center", fontSize:13, flexShrink:0 }}>🔭</div>
        <div style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:"2px 12px 12px 12px",
          padding:"10px 14px", flex:1 }}>
          {msg.loading ? (
            <div style={{ display:"flex", gap:5, alignItems:"center" }}>
              {[0,1,2].map(i => (
                <div key={i} style={{ width:6, height:6, borderRadius:"50%", background:"#8B949E",
                  animation:`blink 1.2s ${i*0.2}s ease-in-out infinite` }} />
              ))}
            </div>
          ) : <div style={{ fontSize:13, color:"#F85149" }}>{msg.error}</div>}
        </div>
      </div>
    );
  }
  const qtColor = QUERY_TYPE_COLORS[resp.query_type] || "#8B949E";
  return (
    <div style={{ display:"flex", gap:10, marginBottom:20 }}>
      <div style={{ width:28, height:28, borderRadius:6, background:"linear-gradient(135deg,#1F6FEB,#3FB950)",
        display:"flex", alignItems:"center", justifyContent:"center", fontSize:13, flexShrink:0, marginTop:1 }}>🔭</div>
      <div style={{ flex:1 }}>
        <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:8 }}>
          <Badge label={resp.query_type} color={qtColor} />
          <ConfidenceMeter value={resp.confidence} />
          {resp.grounded && <Badge label="✓ grounded" color="#3FB950" />}
        </div>
        <div style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:"2px 12px 12px 12px", padding:"12px 16px" }}>
          <div style={{ fontSize:13, color:"#E6EDF3", lineHeight:1.75, whiteSpace:"pre-wrap" }}>{resp.answer}</div>
          {resp.disclaimer && (
            <div style={{ marginTop:10, padding:"8px 10px", background:"#FFA65711",
              border:"1px solid #FFA65733", borderRadius:5, fontSize:11.5, color:"#FFA657" }}>
              ⚠ {resp.disclaimer}
            </div>
          )}
          {resp.issue_solution && <IssueCard solution={resp.issue_solution} />}
          {resp.variables?.length > 0 && (
            <div style={{ marginTop:12 }}>
              <div style={{ fontSize:11, color:"#8B949E", marginBottom:8, textTransform:"uppercase", letterSpacing:"0.07em" }}>
                Variables ({resp.variables.length})
              </div>
              <div style={{ display:"flex", flexWrap:"wrap", gap:6 }}>
                {resp.variables.map((v,i) => (
                  <div key={i} style={{ background:"#0D1117", border:"1px solid #21262D",
                    borderRadius:5, padding:"4px 8px", display:"flex", alignItems:"center", gap:6 }}>
                    <span style={{ color: v.required ? "#F85149" : "#3FB950", fontSize:10 }}>
                      {v.required ? "required" : "optional"}
                    </span>
                    <span style={{ fontFamily:"monospace", fontSize:11, color:"#D2A8FF" }}>{v.name}</span>
                    <span style={{ fontSize:10, color:"#484F58" }}>{v.type}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {resp.sources?.length > 0 && (
            <div style={{ marginTop:12 }}>
              <div style={{ fontSize:11, color:"#8B949E", marginBottom:6, textTransform:"uppercase", letterSpacing:"0.07em" }}>
                Sources ({resp.sources.length})
              </div>
              {resp.sources.slice(0,4).map((src,i) => <SourceChip key={i} source={src} />)}
            </div>
          )}
        </div>
        <div style={{ display:"flex", gap:12, marginTop:6, paddingLeft:4 }}>
          {resp.repo_name && <span style={{ fontSize:10, color:"#484F58", fontFamily:"monospace" }}>{resp.repo_name}</span>}
          {resp.tags_analyzed?.length > 0 && (
            <span style={{ fontSize:10, color:"#484F58", fontFamily:"monospace" }}>@ {resp.tags_analyzed.join(", ")}</span>
          )}
        </div>
      </div>
    </div>
  );
}

// ── GA Panel components ───────────────────────────────────────────────────────

function StageRow({ stageKey, currentStage, label, done }) {
  const stages = ["detecting_ga","scanning_gcp_service","analyzing_changes","creating_branch",
    "implementing_changes","validating_code","checking_pr","creating_pr","updating_pr","done"];
  const idx = stages.indexOf(stageKey);
  const curIdx = stages.indexOf(currentStage);
  const isActive = stageKey === currentStage;
  const isDone = done || curIdx > idx;
  const color = isDone ? "#3FB950" : isActive ? "#FFA657" : "#484F58";
  return (
    <div style={{ display:"flex", alignItems:"center", gap:10, padding:"5px 0" }}>
      <div style={{ width:18, height:18, borderRadius:"50%", border:`1.5px solid ${color}`,
        display:"flex", alignItems:"center", justifyContent:"center", fontSize:10, color, flexShrink:0,
        background: isDone ? "#3FB95011" : isActive ? "#FFA65711" : "transparent" }}>
        {isDone ? "✓" : isActive ? "●" : "○"}
      </div>
      <span style={{ fontSize:11.5, color: isActive ? "#E6EDF3" : isDone ? "#C9D1D9" : "#484F58" }}>
        {label}
      </span>
      {isActive && (
        <div style={{ display:"flex", gap:3, marginLeft:"auto" }}>
          {[0,1,2].map(i => (
            <div key={i} style={{ width:4, height:4, borderRadius:"50%", background:"#FFA657",
              animation:`blink 1.2s ${i*0.2}s ease-in-out infinite` }} />
          ))}
        </div>
      )}
    </div>
  );
}

function FeatureCard({ feature, idx }) {
  const [exp, setExp] = useState(false);
  const impactColor = IMPACT_COLORS[feature.terraform_impact] || "#484F58";
  return (
    <div style={{ background:"#0D1117", border:"1px solid #21262D", borderRadius:8,
      overflow:"hidden", marginBottom:8 }}>
      <div onClick={() => setExp(!exp)} style={{
        padding:"10px 14px", cursor:"pointer", display:"flex", alignItems:"flex-start", gap:10 }}>
        <div style={{ width:20, height:20, borderRadius:4, background: impactColor + "22",
          border:`1px solid ${impactColor}44`, display:"flex", alignItems:"center",
          justifyContent:"center", fontSize:10, color:impactColor, flexShrink:0, marginTop:1 }}>
          {idx + 1}
        </div>
        <div style={{ flex:1 }}>
          <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:3, flexWrap:"wrap" }}>
            <span style={{ fontSize:12.5, color:"#E6EDF3", fontWeight:600 }}>{feature.feature_name}</span>
            <Badge label={feature.terraform_impact.replace("_"," ")} color={impactColor} />
            {feature.ga_confirmed && <Badge label="GA ✓" color="#3FB950" />}
          </div>
          <div style={{ fontSize:11.5, color:"#8B949E", lineHeight:1.5 }}>{feature.description}</div>
        </div>
        <span style={{ color:"#484F58", fontSize:12, flexShrink:0 }}>{exp ? "▲" : "▼"}</span>
      </div>
      {exp && (
        <div style={{ borderTop:"1px solid #21262D", padding:"10px 14px" }}>
          {feature.terraform_resources?.length > 0 && (
            <div style={{ marginBottom:8 }}>
              <div style={{ fontSize:10, color:"#484F58", textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:4 }}>
                Terraform Resources
              </div>
              <div style={{ display:"flex", gap:6, flexWrap:"wrap" }}>
                {feature.terraform_resources.map((r,i) => (
                  <span key={i} style={{ fontFamily:"monospace", fontSize:11, color:"#D2A8FF",
                    background:"#D2A8FF11", padding:"2px 7px", borderRadius:3 }}>{r}</span>
                ))}
              </div>
            </div>
          )}
          {feature.terraform_args?.length > 0 && (
            <div style={{ marginBottom:8 }}>
              <div style={{ fontSize:10, color:"#484F58", textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:4 }}>
                New Arguments
              </div>
              <div style={{ display:"flex", gap:6, flexWrap:"wrap" }}>
                {feature.terraform_args.map((a,i) => (
                  <span key={i} style={{ fontFamily:"monospace", fontSize:11, color:"#58A6FF",
                    background:"#58A6FF11", padding:"2px 7px", borderRadius:3 }}>{a}</span>
                ))}
              </div>
            </div>
          )}
          {feature.announced_date && (
            <div style={{ fontSize:10, color:"#484F58" }}>Announced: {feature.announced_date}</div>
          )}
          {feature.source_url && (
            <a href={feature.source_url} target="_blank" rel="noreferrer"
              style={{ fontSize:10, color:"#58A6FF", display:"block", marginTop:4 }}>
              View source ↗
            </a>
          )}
        </div>
      )}
    </div>
  );
}

function GCPServiceScanPanel({ scan, loading, onScan }) {
  if (loading) {
    return (
      <div style={{ display:"flex", flexDirection:"column", alignItems:"center",
        justifyContent:"center", height:200, gap:12, color:"#484F58" }}>
        <div style={{ fontSize:24 }}>🔍</div>
        <div style={{ fontSize:12 }}>Scanning GCP service for new GA features…</div>
        <div style={{ display:"flex", gap:5 }}>
          {[0,1,2].map(i => <div key={i} style={{ width:6, height:6, borderRadius:"50%",
            background:"#FFA657", animation:`blink 1.2s ${i*0.2}s ease-in-out infinite` }} />)}
        </div>
      </div>
    );
  }
  if (!scan) {
    return (
      <div style={{ display:"flex", flexDirection:"column", alignItems:"center",
        justifyContent:"center", height:200, gap:10, color:"#484F58" }}>
        <div style={{ fontSize:24 }}>📡</div>
        <div style={{ fontSize:12, textAlign:"center", lineHeight:1.6, maxWidth:300 }}>
          Scan the GCP service for new GA features not yet in this module.
        </div>
        <button onClick={onScan} style={{ background:"#1F6FEB22", border:"1px solid #1F6FEB44",
          borderRadius:6, color:"#58A6FF", fontSize:11.5, padding:"6px 16px", cursor:"pointer",
          fontFamily:"inherit" }}>
          Scan GCP Service
        </button>
      </div>
    );
  }
  return (
    <div>
      <div style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:8,
        padding:"12px 16px", marginBottom:12 }}>
        <div style={{ fontSize:11, color:"#484F58", marginBottom:6,
          textTransform:"uppercase", letterSpacing:"0.06em" }}>GCP Service Scan Summary</div>
        <div style={{ fontSize:12.5, color:"#C9D1D9", lineHeight:1.7 }}>{scan.summary}</div>
        <div style={{ display:"flex", gap:16, marginTop:10 }}>
          <div style={{ textAlign:"center" }}>
            <div style={{ fontSize:20, fontWeight:700, color:"#58A6FF" }}>{scan.total_features}</div>
            <div style={{ fontSize:10, color:"#484F58" }}>Total Features</div>
          </div>
          <div style={{ textAlign:"center" }}>
            <div style={{ fontSize:20, fontWeight:700, color:"#3FB950" }}>{scan.actionable_count}</div>
            <div style={{ fontSize:10, color:"#484F58" }}>Actionable Gaps</div>
          </div>
          <div style={{ textAlign:"center" }}>
            <div style={{ fontSize:20, fontWeight:700, color:"#C9D1D9" }}>{scan.module_resources?.length || 0}</div>
            <div style={{ fontSize:10, color:"#484F58" }}>Module Resources</div>
          </div>
        </div>
        {scan.docs_url && (
          <a href={scan.docs_url} target="_blank" rel="noreferrer"
            style={{ fontSize:10, color:"#58A6FF", display:"block", marginTop:8 }}>
            GCP Release Notes ↗
          </a>
        )}
      </div>

      {scan.actionable_features?.length > 0 && (
        <div>
          <div style={{ fontSize:11, color:"#FFA657", marginBottom:8,
            textTransform:"uppercase", letterSpacing:"0.06em" }}>
            ⚡ Actionable Gaps — {scan.actionable_features.length}
          </div>
          {scan.actionable_features.map((f,i) => <FeatureCard key={i} feature={f} idx={i} />)}
        </div>
      )}

      {scan.features?.length > scan.actionable_features?.length && (
        <details style={{ marginTop:12 }}>
          <summary style={{ fontSize:11, color:"#484F58", cursor:"pointer", userSelect:"none" }}>
            All detected features ({scan.features.length})
          </summary>
          <div style={{ marginTop:8 }}>
            {scan.features.map((f,i) => <FeatureCard key={i} feature={f} idx={i} />)}
          </div>
        </details>
      )}

      <button onClick={onScan} style={{ marginTop:12, background:"none",
        border:"1px solid #21262D", borderRadius:5, color:"#484F58", fontSize:10,
        padding:"4px 10px", cursor:"pointer", fontFamily:"inherit" }}>
        ⟳ Re-scan
      </button>
    </div>
  );
}

function GAWorkflowPanel({ repo, health }) {
  const [baseBranch, setBaseBranch] = useState("main");
  const [dryRun, setDryRun] = useState(false);
  const [autoFix, setAutoFix] = useState(true);
  const [running, setRunning] = useState(false);
  const [workflowRun, setWorkflowRun] = useState(null);
  const [gaDetect, setGaDetect] = useState(null);
  const [detectLoading, setDetectLoading] = useState(false);
  const [scanResult, setScanResult] = useState(null);
  const [scanLoading, setScanLoading] = useState(false);
  const [activeTab, setActiveTab] = useState("workflow"); // workflow | gcp_scan

  const ollamaOk = health?.ollama === "running";

  // Auto-detect on repo change
  useEffect(() => {
    if (!repo) return;
    setGaDetect(null);
    setWorkflowRun(null);
    setScanResult(null);
    setDetectLoading(true);
    apiGet(`/detect/${repo.name}`, GA_API)
      .then(setGaDetect)
      .catch(() => {})
      .finally(() => setDetectLoading(false));
  }, [repo?.name]);

  const handleRunWorkflow = async () => {
    if (!repo || running) return;
    setRunning(true);
    setWorkflowRun(null);
    try {
      const result = await apiPost("/workflow", {
        repo_name: repo.name,
        base_branch: baseBranch,
        dry_run: dryRun,
        auto_fix: autoFix,
      }, GA_API);
      setWorkflowRun(result);
    } catch (e) {
      setWorkflowRun({ stage: "failed", error: e.message, logs: [] });
    } finally {
      setRunning(false);
    }
  };

  const handleScanGCP = async () => {
    if (!repo) return;
    setScanLoading(true);
    setScanResult(null);
    try {
      const result = await apiGet(`/scan/${repo.name}`, GA_API);
      setScanResult(result);
    } catch (e) {
      setScanResult({ summary: `Scan failed: ${e.message}`, total_features:0, actionable_count:0 });
    } finally {
      setScanLoading(false);
    }
  };

  if (!repo) {
    return (
      <div style={{ display:"flex", flexDirection:"column", alignItems:"center",
        justifyContent:"center", height:"100%", gap:12, opacity:0.5 }}>
        <div style={{ fontSize:28 }}>🚀</div>
        <div style={{ fontSize:13, color:"#8B949E" }}>Select a repo to run GA Workflow</div>
      </div>
    );
  }

  const color = GCP_COLORS[repo.gcp_product] || GCP_COLORS.default;
  const stage = workflowRun?.stage;
  const isDone = stage === "done";
  const isFailed = stage === "failed";

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      {/* Repo + version header */}
      <div style={{ padding:"14px 20px", borderBottom:"1px solid #21262D",
        background:"#161B22", flexShrink:0 }}>
        <div style={{ display:"flex", alignItems:"center", gap:10, marginBottom:10 }}>
          <div style={{ width:10, height:10, borderRadius:2, background:color }} />
          <span style={{ fontSize:13, fontWeight:700, color:"#E6EDF3" }}>{repo.display_name}</span>
          <span style={{ fontSize:10, color:"#484F58", fontFamily:"monospace" }}>{repo.name}</span>
        </div>
        {detectLoading && (
          <div style={{ fontSize:11, color:"#484F58" }}>Checking provider version…</div>
        )}
        {gaDetect && !detectLoading && (
          <div style={{ display:"flex", gap:16, alignItems:"center", flexWrap:"wrap" }}>
            <div>
              <div style={{ fontSize:10, color:"#484F58" }}>Current</div>
              <div style={{ fontFamily:"monospace", fontSize:12, color:"#C9D1D9" }}>
                v{gaDetect.current_version}
              </div>
            </div>
            <div style={{ fontSize:14, color: gaDetect.upgrade_required ? "#FFA657" : "#3FB950" }}>→</div>
            <div>
              <div style={{ fontSize:10, color:"#484F58" }}>Latest GA</div>
              <div style={{ fontFamily:"monospace", fontSize:12,
                color: gaDetect.upgrade_required ? "#FFA657" : "#3FB950",
                fontWeight:700 }}>
                v{gaDetect.latest_ga_version}
              </div>
            </div>
            {gaDetect.upgrade_required ? (
              <Badge label="UPGRADE AVAILABLE" color="#FFA657" />
            ) : (
              <Badge label="UP TO DATE" color="#3FB950" />
            )}
            {gaDetect.breaking_changes > 0 && (
              <Badge label={`${gaDetect.breaking_changes} breaking`} color="#F85149" />
            )}
            {gaDetect.new_features > 0 && (
              <Badge label={`${gaDetect.new_features} new`} color="#3FB950" />
            )}
          </div>
        )}
      </div>

      {/* Tabs: Workflow | GCP Service Scan */}
      <div style={{ display:"flex", borderBottom:"1px solid #21262D",
        background:"#0D1117", flexShrink:0 }}>
        {[
          { key:"workflow", label:"🔄 GA Workflow" },
          { key:"gcp_scan", label:"📡 GCP Service Scan" },
        ].map(t => (
          <button key={t.key} onClick={() => setActiveTab(t.key)} style={{
            background:"none", border:"none", cursor:"pointer", padding:"9px 18px",
            fontSize:11.5, fontFamily:"inherit", fontWeight: activeTab===t.key ? 600 : 400,
            color: activeTab===t.key ? "#E6EDF3" : "#484F58",
            borderBottom:`2px solid ${activeTab===t.key ? color : "transparent"}`,
            transition:"all 0.12s",
          }}>{t.label}</button>
        ))}
      </div>

      <div style={{ flex:1, overflowY:"auto", padding:"16px 20px" }}>

        {/* ── Workflow tab ── */}
        {activeTab === "workflow" && (
          <>
            {/* Config row */}
            {!running && !workflowRun && (
              <div style={{ display:"flex", gap:10, alignItems:"center",
                marginBottom:16, flexWrap:"wrap" }}>
                <div style={{ display:"flex", alignItems:"center", gap:6 }}>
                  <span style={{ fontSize:11, color:"#484F58" }}>Base branch:</span>
                  <input value={baseBranch} onChange={e => setBaseBranch(e.target.value)}
                    style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:4,
                      color:"#E6EDF3", fontSize:11, padding:"3px 8px", fontFamily:"monospace",
                      width:80 }} />
                </div>
                <label style={{ display:"flex", alignItems:"center", gap:5,
                  fontSize:11, color:"#8B949E", cursor:"pointer" }}>
                  <input type="checkbox" checked={dryRun} onChange={e => setDryRun(e.target.checked)}
                    style={{ accentColor:"#58A6FF" }} />
                  Dry run
                </label>
                <label style={{ display:"flex", alignItems:"center", gap:5,
                  fontSize:11, color:"#8B949E", cursor:"pointer" }}>
                  <input type="checkbox" checked={autoFix} onChange={e => setAutoFix(e.target.checked)}
                    style={{ accentColor:"#3FB950" }} />
                  Auto-fix
                </label>
                <button onClick={handleRunWorkflow}
                  disabled={running || !ollamaOk}
                  style={{
                    marginLeft:"auto", background: ollamaOk ? color + "22" : "#21262D",
                    border:`1px solid ${ollamaOk ? color + "66" : "#21262D"}`,
                    borderRadius:6, color: ollamaOk ? color : "#484F58",
                    fontSize:12, padding:"6px 18px", cursor: ollamaOk ? "pointer" : "not-allowed",
                    fontFamily:"inherit", fontWeight:600,
                  }}>
                  🚀 Run GA Workflow
                </button>
              </div>
            )}

            {/* Running spinner */}
            {running && (
              <div style={{ display:"flex", flexDirection:"column", alignItems:"center",
                justifyContent:"center", padding:"24px 0", gap:12 }}>
                <div style={{ fontSize:24 }}>⚙️</div>
                <div style={{ fontSize:12, color:"#FFA657" }}>Running GA Workflow…</div>
                <div style={{ fontSize:11, color:"#484F58" }}>This may take 2–5 minutes</div>
              </div>
            )}

            {/* Workflow result */}
            {workflowRun && (
              <div>
                {/* Status banner */}
                <div style={{
                  background: isDone ? "#3FB95011" : isFailed ? "#F8514911" : "#FFA65711",
                  border:`1px solid ${isDone ? "#3FB95033" : isFailed ? "#F8514933" : "#FFA65733"}`,
                  borderRadius:8, padding:"12px 16px", marginBottom:16,
                  display:"flex", alignItems:"center", gap:10,
                }}>
                  <span style={{ fontSize:18 }}>{isDone ? "✅" : isFailed ? "❌" : "⚠️"}</span>
                  <div>
                    <div style={{ fontSize:13, fontWeight:700,
                      color: isDone ? "#3FB950" : isFailed ? "#F85149" : "#FFA657" }}>
                      {isDone ? "Workflow Complete" : isFailed ? "Workflow Failed" : "Completed with Warnings"}
                    </div>
                    {workflowRun.pr_result?.pr_url && (
                      <a href={workflowRun.pr_result.pr_url} target="_blank" rel="noreferrer"
                        style={{ fontSize:11, color:"#58A6FF" }}>
                        PR #{workflowRun.pr_result.pr_number}: {workflowRun.pr_result.pr_url} ↗
                      </a>
                    )}
                    {workflowRun.error && (
                      <div style={{ fontSize:11, color:"#F85149", marginTop:3 }}>{workflowRun.error}</div>
                    )}
                  </div>
                  <button onClick={() => { setWorkflowRun(null); }} style={{
                    marginLeft:"auto", background:"none", border:"1px solid #21262D",
                    borderRadius:4, color:"#484F58", fontSize:10, padding:"3px 8px",
                    cursor:"pointer", fontFamily:"inherit" }}>
                    Reset
                  </button>
                </div>

                {/* Stage progress */}
                <div style={{ background:"#161B22", border:"1px solid #21262D",
                  borderRadius:8, padding:"12px 16px", marginBottom:16 }}>
                  <div style={{ fontSize:10, color:"#484F58", textTransform:"uppercase",
                    letterSpacing:"0.06em", marginBottom:10 }}>Pipeline Stages</div>
                  {[
                    ["detecting_ga","1. Detect GA release"],
                    ["scanning_gcp_service","1b. Scan GCP service"],
                    ["analyzing_changes","2. Analyse changes"],
                    ["creating_branch","3. Create branch"],
                    ["implementing_changes","4. Implement changes"],
                    ["validating_code","5. Validate code"],
                    ["checking_pr","6. Check provider compat"],
                    ["creating_pr","7. Create / Update PR"],
                  ].map(([key, label]) => (
                    <StageRow key={key} stageKey={key} currentStage={workflowRun.stage}
                      label={label} done={isDone} />
                  ))}
                </div>

                {/* Validation result */}
                {workflowRun.validation_result && (
                  <div style={{ background:"#161B22", border:"1px solid #21262D",
                    borderRadius:8, padding:"12px 16px", marginBottom:16 }}>
                    <div style={{ fontSize:10, color:"#484F58", textTransform:"uppercase",
                      letterSpacing:"0.06em", marginBottom:8 }}>Validation</div>
                    <div style={{ display:"flex", gap:16 }}>
                      <div>
                        <div style={{ fontSize:18, fontWeight:700,
                          color: workflowRun.validation_result.error_count > 0 ? "#F85149" : "#3FB950" }}>
                          {workflowRun.validation_result.error_count}
                        </div>
                        <div style={{ fontSize:10, color:"#484F58" }}>Errors</div>
                      </div>
                      <div>
                        <div style={{ fontSize:18, fontWeight:700, color:"#FFA657" }}>
                          {workflowRun.validation_result.warning_count}
                        </div>
                        <div style={{ fontSize:10, color:"#484F58" }}>Warnings</div>
                      </div>
                      {workflowRun.validation_result.reports?.map((r,i) => (
                        <div key={i} style={{ fontSize:11, color: r.passed ? "#3FB950" : "#F85149" }}>
                          {r.passed ? "✓" : "✗"} {r.validator_name}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* GCP Service scan inside workflow result */}
                {workflowRun.gcp_service_scan && workflowRun.gcp_service_scan.actionable_count > 0 && (
                  <div style={{ background:"#161B22", border:`1px solid ${color}33`,
                    borderRadius:8, padding:"12px 16px", marginBottom:16 }}>
                    <div style={{ fontSize:10, color:"#484F58", textTransform:"uppercase",
                      letterSpacing:"0.06em", marginBottom:8 }}>
                      GCP Service Gaps Found
                    </div>
                    <div style={{ fontSize:12, color:"#FFA657", marginBottom:8 }}>
                      {workflowRun.gcp_service_scan.actionable_count} new GCP features need module updates
                    </div>
                    {workflowRun.gcp_service_scan.actionable_features?.slice(0,3).map((f,i) => (
                      <div key={i} style={{ display:"flex", gap:8, marginBottom:6, alignItems:"flex-start" }}>
                        <Badge label={f.terraform_impact.replace("_"," ")}
                          color={IMPACT_COLORS[f.terraform_impact] || "#484F58"} />
                        <span style={{ fontSize:11.5, color:"#C9D1D9" }}>{f.feature_name}: {f.description?.slice(0,80)}</span>
                      </div>
                    ))}
                    <button onClick={() => setActiveTab("gcp_scan")}
                      style={{ marginTop:6, background:"none", border:"1px solid #21262D",
                        borderRadius:4, color:"#58A6FF", fontSize:10, padding:"3px 8px",
                        cursor:"pointer", fontFamily:"inherit" }}>
                      View full GCP scan →
                    </button>
                  </div>
                )}

                {/* Logs */}
                <details>
                  <summary style={{ fontSize:11, color:"#484F58", cursor:"pointer",
                    userSelect:"none", marginBottom:6 }}>
                    Pipeline logs ({workflowRun.logs?.length || 0})
                  </summary>
                  <div style={{ background:"#0D1117", border:"1px solid #21262D",
                    borderRadius:6, padding:"8px 10px", maxHeight:300, overflowY:"auto",
                    marginTop:6 }}>
                    {(workflowRun.logs || []).map((log, i) => (
                      <div key={i} style={{
                        fontSize:10.5, fontFamily:"monospace", lineHeight:1.6,
                        color: log.level==="error" ? "#F85149" : log.level==="warning" ? "#FFA657" : "#8B949E",
                        paddingLeft: log.message.startsWith("  ") ? 16 : 0,
                      }}>
                        <span style={{ color:"#484F58" }}>
                          [{log.stage?.replace("_"," ")}]
                        </span>{" "}
                        {log.level==="error" ? "✗" : log.level==="warning" ? "⚠" : "→"} {log.message}
                      </div>
                    ))}
                  </div>
                </details>
              </div>
            )}

            {/* No workflow run yet + detect summary */}
            {!running && !workflowRun && gaDetect && gaDetect.changes?.length > 0 && (
              <div>
                <div style={{ fontSize:11, color:"#8B949E", marginBottom:10,
                  textTransform:"uppercase", letterSpacing:"0.06em" }}>
                  Detected Changes ({gaDetect.changes.length})
                </div>
                {gaDetect.changes.slice(0,8).map((c,i) => (
                  <div key={i} style={{ display:"flex", gap:8, marginBottom:6, alignItems:"flex-start",
                    padding:"6px 10px", background:"#161B22", borderRadius:6,
                    border:"1px solid #21262D" }}>
                    <Badge label={c.change_type.replace("_"," ")}
                      color={c.breaking ? "#F85149" : "#58A6FF"} />
                    <span style={{ fontFamily:"monospace", fontSize:11, color:"#D2A8FF",
                      flexShrink:0 }}>{c.resource_type}</span>
                    {c.attribute && (
                      <span style={{ fontSize:11, color:"#484F58" }}>.{c.attribute}</span>
                    )}
                    <span style={{ fontSize:11, color:"#8B949E", flex:1 }}>{c.description?.slice(0,80)}</span>
                    {c.breaking && <Badge label="breaking" color="#F85149" />}
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        {/* ── GCP Service Scan tab ── */}
        {activeTab === "gcp_scan" && (
          <GCPServiceScanPanel
            scan={scanResult}
            loading={scanLoading}
            onScan={handleScanGCP}
          />
        )}
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
  const [sidebarTab, setSidebarTab] = useState("repos");
  const [mainView, setMainView] = useState("chat");   // "chat" | "ga"
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    apiGet("/repos").then(d => {
      setRepos(d);
      if (d.length > 0) { setSelectedRepo(d[0]); setSelectedTag(d[0].latest_tag); }
    }).catch(() => {});
    apiGet("/health").then(setHealth).catch(() => {});
  }, []);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior:"smooth" }); }, [messages]);

  const handleSend = useCallback(async () => {
    if (!input.trim() || loading) return;
    const question = input.trim();
    setInput("");
    const userMsg = { id:Date.now(), role:"user", content:question,
      meta: selectedRepo ? `${selectedRepo.name} @ ${selectedTag||"latest"}` : "All repos" };
    const agentMsg = { id:Date.now()+1, role:"agent", loading:true, response:null, error:null };
    setMessages(prev => [...prev, userMsg, agentMsg]);
    setLoading(true);
    try {
      const resp = await apiPost("/query", {
        question, repo_name: selectedRepo?.name||null,
        tag: selectedTag||null, strict_mode:true,
      });
      setMessages(prev => prev.map(m => m.id===agentMsg.id ? {...m, loading:false, response:resp} : m));
    } catch (e) {
      setMessages(prev => prev.map(m => m.id===agentMsg.id
        ? {...m, loading:false, error:`Error: ${e.message}. Is TerraScope running?`} : m));
    } finally {
      setLoading(false);
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [input, loading, selectedRepo, selectedTag]);

  const handleIndex = async () => {
    setIndexing(true);
    try {
      await apiPost("/index", { repo_name: selectedRepo?.name||null, force:false });
      setTimeout(async () => { const d = await apiGet("/repos"); setRepos(d); setIndexing(false); }, 2000);
    } catch { setIndexing(false); }
  };

  const ollamaOk = health?.ollama === "running";

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100vh",
      background:"#0D1117", color:"#C9D1D9",
      fontFamily:"'JetBrains Mono','Fira Code','SF Mono',monospace" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap');
        * { box-sizing:border-box; margin:0; padding:0; }
        ::-webkit-scrollbar { width:6px; }
        ::-webkit-scrollbar-track { background:#0D1117; }
        ::-webkit-scrollbar-thumb { background:#21262D; border-radius:3px; }
        @keyframes blink { 0%,80%,100%{opacity:.2;transform:scale(.8)} 40%{opacity:1;transform:scale(1)} }
        @keyframes pulse { 0%,100%{box-shadow:0 0 0 0 currentColor} 50%{box-shadow:0 0 0 4px transparent} }
        textarea:focus { outline:none; }
        button:hover { opacity:.85; }
        details summary::-webkit-details-marker { display:none; }
      `}</style>

      {/* ── Top bar ── */}
      <div style={{ height:48, borderBottom:"1px solid #21262D", display:"flex",
        alignItems:"center", padding:"0 16px", gap:14, background:"#161B22", flexShrink:0 }}>
        <div style={{ display:"flex", alignItems:"center", gap:8 }}>
          <div style={{ width:24, height:24, borderRadius:5,
            background:"linear-gradient(135deg,#1F6FEB,#3FB950)",
            display:"flex", alignItems:"center", justifyContent:"center", fontSize:12 }}>🔭</div>
          <span style={{ fontWeight:700, fontSize:14, color:"#E6EDF3" }}>TerraScope</span>
          <span style={{ fontSize:10, color:"#484F58", padding:"1px 5px",
            background:"#21262D", borderRadius:3 }}>v1.0</span>
        </div>

        {/* View toggle */}
        <div style={{ display:"flex", gap:2, background:"#0D1117",
          borderRadius:6, padding:3, border:"1px solid #21262D" }}>
          {[
            { key:"chat", label:"💬 Chat" },
            { key:"ga",   label:"🚀 GA Workflow" },
          ].map(v => (
            <button key={v.key} onClick={() => setMainView(v.key)} style={{
              background: mainView===v.key ? "#161B22" : "transparent",
              border:"none", cursor:"pointer", padding:"4px 12px", borderRadius:4,
              fontSize:11, color: mainView===v.key ? "#E6EDF3" : "#484F58",
              fontFamily:"inherit", fontWeight: mainView===v.key ? 600 : 400,
              transition:"all 0.12s",
            }}>{v.label}</button>
          ))}
        </div>

        <div style={{ flex:1 }} />

        <div style={{ display:"flex", alignItems:"center", gap:6 }}>
          <StatusDot status={ollamaOk ? "ready" : "unreachable"} />
          <span style={{ fontSize:11, color:"#8B949E" }}>
            {health ? (ollamaOk ? health.model : "Ollama offline") : "Checking…"}
          </span>
        </div>

        <div style={{ fontSize:10, color:"#3FB950", background:"#3FB95011",
          border:"1px solid #3FB95033", padding:"2px 8px", borderRadius:3 }}>
          STRICT GROUNDING
        </div>

        <button onClick={handleIndex} disabled={indexing} style={{
          background: indexing ? "#21262D" : "#1F6FEB22",
          border:"1px solid #1F6FEB44", borderRadius:5,
          color:"#58A6FF", fontSize:11, padding:"5px 12px", cursor:"pointer",
          display:"flex", alignItems:"center", gap:6,
        }}>
          {indexing ? <><StatusDot status="indexing" /> Indexing…</> : <><span>⟳</span> Index Repos</>}
        </button>
      </div>

      {/* ── Body ── */}
      <div style={{ display:"flex", flex:1, overflow:"hidden" }}>

        {/* ── Sidebar ── */}
        <div style={{ width:220, borderRight:"1px solid #21262D", display:"flex",
          flexDirection:"column", background:"#161B22", flexShrink:0 }}>
          <div style={{ display:"flex", borderBottom:"1px solid #21262D" }}>
            {["repos","tags"].map(tab => (
              <button key={tab} onClick={() => setSidebarTab(tab)} style={{
                flex:1, background:"none", border:"none", cursor:"pointer",
                padding:"8px 0", fontSize:10.5, fontFamily:"inherit",
                textTransform:"uppercase", letterSpacing:"0.06em",
                color: sidebarTab===tab ? "#58A6FF" : "#484F58",
                borderBottom:`2px solid ${sidebarTab===tab ? "#58A6FF" : "transparent"}`,
              }}>{tab}</button>
            ))}
          </div>

          <div style={{ flex:1, overflowY:"auto" }}>
            {sidebarTab==="repos" && (
              <>
                {repos.length===0 ? (
                  <div style={{ padding:12, fontSize:11, color:"#484F58", lineHeight:1.6 }}>
                    No repos configured.<br />Edit terrascope.config.yaml to add repos.
                  </div>
                ) : repos.map(repo => (
                  <RepoCard key={repo.name} repo={repo}
                    selected={selectedRepo?.name===repo.name}
                    onSelect={r => { setSelectedRepo(r); setSelectedTag(r.latest_tag); setSidebarTab("tags"); }} />
                ))}
              </>
            )}
            {sidebarTab==="tags" && selectedRepo && (
              <>
                <div style={{ padding:"8px 12px", fontSize:10, color:"#484F58",
                  textTransform:"uppercase", letterSpacing:"0.06em", borderBottom:"1px solid #21262D" }}>
                  {selectedRepo.display_name} — {selectedRepo.tags?.length||0} tags
                </div>
                {(selectedRepo.tags||[]).map(tag => (
                  <Tag key={tag} tag={tag} selected={selectedTag===tag}
                    indexed={selectedRepo.indexed_tags?.includes(tag)}
                    onClick={() => setSelectedTag(tag)} />
                ))}
              </>
            )}
          </div>

          {selectedRepo && (
            <div style={{ borderTop:"1px solid #21262D", padding:"10px 12px" }}>
              <div style={{ fontSize:10, color:"#484F58", lineHeight:1.6 }}>
                <div style={{ color:"#8B949E", marginBottom:2 }}>{selectedRepo.display_name}</div>
                <div>{selectedRepo.indexed_tags?.length||0}/{selectedRepo.tags?.length||0} tags indexed</div>
              </div>
            </div>
          )}
        </div>

        {/* ── Main area: Chat or GA ── */}
        {mainView === "chat" ? (
          <div style={{ flex:1, display:"flex", flexDirection:"column", overflow:"hidden" }}>
            {/* Context bar */}
            <div style={{ height:36, borderBottom:"1px solid #21262D", display:"flex",
              alignItems:"center", padding:"0 16px", gap:10, background:"#0D1117", flexShrink:0 }}>
              {selectedRepo ? (
                <>
                  <span style={{ width:8, height:8, borderRadius:2, flexShrink:0,
                    background: GCP_COLORS[selectedRepo.gcp_product]||GCP_COLORS.default }} />
                  <span style={{ fontSize:11.5, color:"#C9D1D9" }}>{selectedRepo.display_name}</span>
                  {selectedTag && (
                    <>
                      <span style={{ color:"#21262D" }}>›</span>
                      <span style={{ fontFamily:"monospace", fontSize:11, color:"#58A6FF",
                        background:"#1F6FEB11", padding:"1px 6px", borderRadius:3 }}>{selectedTag}</span>
                      <StatusDot status={selectedRepo.indexed_tags?.includes(selectedTag) ? "ready" : "not_indexed"} />
                    </>
                  )}
                </>
              ) : (
                <span style={{ fontSize:11, color:"#484F58" }}>Select a repo from the sidebar</span>
              )}
            </div>

            {/* Messages */}
            <div style={{ flex:1, overflowY:"auto", padding:"20px 24px" }}>
              {messages.length===0 && (
                <div style={{ display:"flex", flexDirection:"column", alignItems:"center",
                  justifyContent:"center", height:"100%", gap:16, opacity:0.5 }}>
                  <div style={{ width:56, height:56, borderRadius:14,
                    background:"linear-gradient(135deg,#1F6FEB22,#3FB95022)",
                    border:"1px solid #1F6FEB33",
                    display:"flex", alignItems:"center", justifyContent:"center", fontSize:28 }}>🔭</div>
                  <div style={{ textAlign:"center" }}>
                    <div style={{ fontSize:14, color:"#8B949E", marginBottom:6 }}>TerraScope ready</div>
                    <div style={{ fontSize:11.5, color:"#484F58", lineHeight:1.7 }}>
                      Ask about any tag, variable, resource,<br />
                      issue, or change across your Terraform modules.
                    </div>
                  </div>
                  <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:6, maxWidth:480 }}>
                    {[
                      "What GCP resources does this module create?",
                      "What variables are required?",
                      "What changed between v1.0 and v2.0?",
                      "Why does plan fail with 403 on BigQuery?",
                    ].map((q,i) => (
                      <button key={i} onClick={() => setInput(q)} style={{
                        background:"#161B22", border:"1px solid #21262D", borderRadius:6,
                        padding:"8px 10px", cursor:"pointer", fontSize:11.5, color:"#8B949E",
                        textAlign:"left", fontFamily:"inherit", lineHeight:1.4 }}>{q}</button>
                    ))}
                  </div>
                </div>
              )}
              {messages.map(msg => <Message key={msg.id} msg={msg} />)}
              <div ref={bottomRef} />
            </div>

            {/* Input */}
            <div style={{ borderTop:"1px solid #21262D", padding:"12px 16px",
              background:"#0D1117", flexShrink:0 }}>
              <div style={{ display:"flex", gap:10, alignItems:"flex-end",
                background:"#161B22", border:`1px solid ${loading ? "#1F6FEB44" : "#21262D"}`,
                borderRadius:8, padding:"10px 14px", transition:"border-color 0.2s" }}>
                <textarea ref={inputRef} value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={e => { if (e.key==="Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
                  placeholder={selectedRepo
                    ? `Ask about ${selectedRepo.display_name} @ ${selectedTag||"latest"}…`
                    : "Select a repo first…"}
                  disabled={loading || !selectedRepo} rows={1}
                  style={{ flex:1, background:"none", border:"none", color:"#E6EDF3",
                    fontSize:13, fontFamily:"inherit", resize:"none", lineHeight:1.5,
                    maxHeight:120, overflowY:"auto", opacity: !selectedRepo ? 0.4 : 1 }} />
                <button onClick={handleSend}
                  disabled={loading || !input.trim() || !selectedRepo}
                  style={{ width:32, height:32, borderRadius:6,
                    background: loading ? "#21262D" : "#1F6FEB", border:"none", cursor:"pointer",
                    display:"flex", alignItems:"center", justifyContent:"center", fontSize:14,
                    flexShrink:0,
                    opacity: (!input.trim()||!selectedRepo) ? 0.4 : 1, transition:"all 0.15s" }}>
                  {loading ? "⋯" : "↑"}
                </button>
              </div>
              <div style={{ display:"flex", justifyContent:"space-between", marginTop:6, paddingLeft:2 }}>
                <span style={{ fontSize:10, color:"#484F58" }}>Enter to send · Shift+Enter for newline</span>
                <span style={{ fontSize:10, color:"#484F58" }}>
                  Strict mode · {ollamaOk ? "🟢 Ollama" : "🔴 Ollama offline"}
                </span>
              </div>
            </div>
          </div>
        ) : (
          /* ── GA Workflow main view ── */
          <div style={{ flex:1, overflow:"hidden" }}>
            <GAWorkflowPanel repo={selectedRepo} health={health} />
          </div>
        )}
      </div>
    </div>
  );
}
