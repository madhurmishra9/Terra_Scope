import { useState, useEffect, useRef, useCallback } from "react";

// ── API client ────────────────────────────────────────────────────────────────
const API    = "http://localhost:8000/api";
const GA_API = "http://localhost:8000/api/ga";

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
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || `${path} → ${r.status}`);
  }
  return r.json();
}
async function apiUpload(path, formData) {
  const r = await fetch(`${API}${path}`, { method: "POST", body: formData });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || `Upload → ${r.status}`);
  }
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
const PROVIDER_LABELS = { google:"GCP", aws:"AWS", azurerm:"Azure" };
const PROVIDER_COLORS = { google:"#4285F4", aws:"#FF9900", azurerm:"#0078D4" };
const MODE_LABELS = {
  new_product:"New Product",
  from_document:"From Document",
  from_module:"From Module",
  self_curation:"Self-Curation",
};

// ── Shared micro-components ───────────────────────────────────────────────────
function StatusDot({ status }) {
  const colors = { ready:"#3FB950", indexing:"#FFA657", not_indexed:"#484F58",
    idle:"#484F58", done:"#3FB950", unreachable:"#F85149", running:"#FFA657",
    generating:"#FFA657", error:"#F85149" };
  const color = colors[status] || "#484F58";
  const pulse = ["indexing","running","generating"].includes(status);
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
        </div>
      )}
    </div>
  );
}

function GCPServiceScanPanel({ scan, loading, onScan }) {
  if (loading) return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", height:200, gap:12, color:"#484F58" }}>
      <div style={{ fontSize:24 }}>🔍</div>
      <div style={{ fontSize:12 }}>Scanning GCP service for new GA features…</div>
      <div style={{ display:"flex", gap:5 }}>
        {[0,1,2].map(i => <div key={i} style={{ width:6, height:6, borderRadius:"50%", background:"#FFA657", animation:`blink 1.2s ${i*0.2}s ease-in-out infinite` }} />)}
      </div>
    </div>
  );
  if (!scan) return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", height:200, gap:10, color:"#484F58" }}>
      <div style={{ fontSize:24 }}>📡</div>
      <div style={{ fontSize:12, textAlign:"center", lineHeight:1.6, maxWidth:300 }}>
        Scan the GCP service for new GA features not yet in this module.
      </div>
      <button onClick={onScan} style={{ background:"#1F6FEB22", border:"1px solid #1F6FEB44", borderRadius:6, color:"#58A6FF", fontSize:11.5, padding:"6px 16px", cursor:"pointer", fontFamily:"inherit" }}>
        Scan GCP Service
      </button>
    </div>
  );
  return (
    <div>
      <div style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:8, padding:"12px 16px", marginBottom:12 }}>
        <div style={{ fontSize:11, color:"#484F58", marginBottom:6, textTransform:"uppercase", letterSpacing:"0.06em" }}>GCP Service Scan Summary</div>
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
      </div>
      {scan.actionable_features?.length > 0 && (
        <div>
          <div style={{ fontSize:11, color:"#FFA657", marginBottom:8, textTransform:"uppercase", letterSpacing:"0.06em" }}>
            ⚡ Actionable Gaps — {scan.actionable_features.length}
          </div>
          {scan.actionable_features.map((f,i) => <FeatureCard key={i} feature={f} idx={i} />)}
        </div>
      )}
      <button onClick={onScan} style={{ marginTop:12, background:"none", border:"1px solid #21262D", borderRadius:5, color:"#484F58", fontSize:10, padding:"4px 10px", cursor:"pointer", fontFamily:"inherit" }}>
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
  const [activeTab, setActiveTab] = useState("workflow");
  const ollamaOk = health?.ollama === "running";

  useEffect(() => {
    if (!repo) return;
    setGaDetect(null); setWorkflowRun(null); setScanResult(null); setDetectLoading(true);
    apiGet(`/detect/${repo.name}`, GA_API).then(setGaDetect).catch(() => {}).finally(() => setDetectLoading(false));
  }, [repo?.name]);

  const handleRunWorkflow = async () => {
    if (!repo || running) return;
    setRunning(true); setWorkflowRun(null);
    try {
      const result = await apiPost("/workflow", { repo_name:repo.name, base_branch:baseBranch, dry_run:dryRun, auto_fix:autoFix }, GA_API);
      setWorkflowRun(result);
    } catch (e) { setWorkflowRun({ stage:"failed", error:e.message, logs:[] }); }
    finally { setRunning(false); }
  };
  const handleScanGCP = async () => {
    if (!repo) return;
    setScanLoading(true); setScanResult(null);
    try { setScanResult(await apiGet(`/scan/${repo.name}`, GA_API)); }
    catch (e) { setScanResult({ summary:`Scan failed: ${e.message}`, total_features:0, actionable_count:0 }); }
    finally { setScanLoading(false); }
  };

  if (!repo) return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", height:"100%", gap:12, opacity:0.5 }}>
      <div style={{ fontSize:28 }}>🚀</div>
      <div style={{ fontSize:13, color:"#8B949E" }}>Select a repo to run GA Workflow</div>
    </div>
  );

  const color = GCP_COLORS[repo.gcp_product] || GCP_COLORS.default;
  const stage = workflowRun?.stage;
  const isDone = stage === "done"; const isFailed = stage === "failed";

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      <div style={{ padding:"14px 20px", borderBottom:"1px solid #21262D", background:"#161B22", flexShrink:0 }}>
        <div style={{ display:"flex", alignItems:"center", gap:10, marginBottom:10 }}>
          <div style={{ width:10, height:10, borderRadius:2, background:color }} />
          <span style={{ fontSize:13, fontWeight:700, color:"#E6EDF3" }}>{repo.display_name}</span>
          <span style={{ fontSize:10, color:"#484F58", fontFamily:"monospace" }}>{repo.name}</span>
        </div>
        {detectLoading && <div style={{ fontSize:11, color:"#484F58" }}>Checking provider version…</div>}
        {gaDetect && !detectLoading && (
          <div style={{ display:"flex", gap:16, alignItems:"center", flexWrap:"wrap" }}>
            <div><div style={{ fontSize:10, color:"#484F58" }}>Current</div><div style={{ fontFamily:"monospace", fontSize:12, color:"#C9D1D9" }}>v{gaDetect.current_version}</div></div>
            <div style={{ fontSize:14, color: gaDetect.upgrade_required ? "#FFA657" : "#3FB950" }}>→</div>
            <div><div style={{ fontSize:10, color:"#484F58" }}>Latest GA</div><div style={{ fontFamily:"monospace", fontSize:12, color: gaDetect.upgrade_required ? "#FFA657" : "#3FB950", fontWeight:700 }}>v{gaDetect.latest_ga_version}</div></div>
            {gaDetect.upgrade_required ? <Badge label="UPGRADE AVAILABLE" color="#FFA657" /> : <Badge label="UP TO DATE" color="#3FB950" />}
            {gaDetect.breaking_changes > 0 && <Badge label={`${gaDetect.breaking_changes} breaking`} color="#F85149" />}
            {gaDetect.new_features > 0 && <Badge label={`${gaDetect.new_features} new`} color="#3FB950" />}
          </div>
        )}
      </div>
      <div style={{ display:"flex", borderBottom:"1px solid #21262D", background:"#0D1117", flexShrink:0 }}>
        {[{key:"workflow",label:"🔄 GA Workflow"},{key:"gcp_scan",label:"📡 GCP Scan"}].map(t => (
          <button key={t.key} onClick={() => setActiveTab(t.key)} style={{
            background:"none", border:"none", cursor:"pointer", padding:"9px 18px", fontSize:11.5,
            fontFamily:"inherit", fontWeight: activeTab===t.key ? 600 : 400,
            color: activeTab===t.key ? "#E6EDF3" : "#484F58",
            borderBottom:`2px solid ${activeTab===t.key ? color : "transparent"}`, transition:"all 0.12s",
          }}>{t.label}</button>
        ))}
      </div>
      <div style={{ flex:1, overflowY:"auto", padding:"16px 20px" }}>
        {activeTab==="workflow" && (
          <>
            {!running && !workflowRun && (
              <div style={{ display:"flex", gap:10, alignItems:"center", marginBottom:16, flexWrap:"wrap" }}>
                <div style={{ display:"flex", alignItems:"center", gap:6 }}>
                  <span style={{ fontSize:11, color:"#484F58" }}>Base branch:</span>
                  <input value={baseBranch} onChange={e => setBaseBranch(e.target.value)} style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:4, color:"#E6EDF3", fontSize:11, padding:"3px 8px", fontFamily:"monospace", width:80 }} />
                </div>
                <label style={{ display:"flex", alignItems:"center", gap:5, fontSize:11, color:"#8B949E", cursor:"pointer" }}>
                  <input type="checkbox" checked={dryRun} onChange={e => setDryRun(e.target.checked)} style={{ accentColor:"#58A6FF" }} /> Dry run
                </label>
                <label style={{ display:"flex", alignItems:"center", gap:5, fontSize:11, color:"#8B949E", cursor:"pointer" }}>
                  <input type="checkbox" checked={autoFix} onChange={e => setAutoFix(e.target.checked)} style={{ accentColor:"#3FB950" }} /> Auto-fix
                </label>
                <button onClick={handleRunWorkflow} disabled={running || !ollamaOk} style={{ marginLeft:"auto", background: ollamaOk ? color+"22" : "#21262D", border:`1px solid ${ollamaOk ? color+"66" : "#21262D"}`, borderRadius:6, color: ollamaOk ? color : "#484F58", fontSize:12, padding:"6px 18px", cursor: ollamaOk ? "pointer" : "not-allowed", fontFamily:"inherit", fontWeight:600 }}>
                  🚀 Run GA Workflow
                </button>
              </div>
            )}
            {running && (
              <div style={{ display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", padding:"24px 0", gap:12 }}>
                <div style={{ fontSize:24 }}>⚙️</div>
                <div style={{ fontSize:12, color:"#FFA657" }}>Running GA Workflow…</div>
                <div style={{ fontSize:11, color:"#484F58" }}>This may take 2–5 minutes</div>
              </div>
            )}
            {workflowRun && (
              <div>
                <div style={{ background: isDone ? "#3FB95011" : isFailed ? "#F8514911" : "#FFA65711", border:`1px solid ${isDone ? "#3FB95033" : isFailed ? "#F8514933" : "#FFA65733"}`, borderRadius:8, padding:"12px 16px", marginBottom:16, display:"flex", alignItems:"center", gap:10 }}>
                  <span style={{ fontSize:18 }}>{isDone ? "✅" : isFailed ? "❌" : "⚠️"}</span>
                  <div>
                    <div style={{ fontSize:13, fontWeight:700, color: isDone ? "#3FB950" : isFailed ? "#F85149" : "#FFA657" }}>
                      {isDone ? "Workflow Complete" : isFailed ? "Workflow Failed" : "Completed with Warnings"}
                    </div>
                    {workflowRun.pr_result?.pr_url && <a href={workflowRun.pr_result.pr_url} target="_blank" rel="noreferrer" style={{ fontSize:11, color:"#58A6FF" }}>PR #{workflowRun.pr_result.pr_number} ↗</a>}
                    {workflowRun.error && <div style={{ fontSize:11, color:"#F85149", marginTop:3 }}>{workflowRun.error}</div>}
                  </div>
                  <button onClick={() => setWorkflowRun(null)} style={{ marginLeft:"auto", background:"none", border:"1px solid #21262D", borderRadius:4, color:"#484F58", fontSize:10, padding:"3px 8px", cursor:"pointer", fontFamily:"inherit" }}>Reset</button>
                </div>
                <div style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:8, padding:"12px 16px", marginBottom:16 }}>
                  <div style={{ fontSize:10, color:"#484F58", textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:10 }}>Pipeline Stages</div>
                  {[["detecting_ga","1. Detect GA release"],["scanning_gcp_service","1b. Scan GCP service"],["analyzing_changes","2. Analyse changes"],["creating_branch","3. Create branch"],["implementing_changes","4. Implement changes"],["validating_code","5. Validate code"],["checking_pr","6. Check provider compat"],["creating_pr","7. Create / Update PR"]].map(([key,label]) => (
                    <StageRow key={key} stageKey={key} currentStage={workflowRun.stage} label={label} done={isDone} />
                  ))}
                </div>
                <details>
                  <summary style={{ fontSize:11, color:"#484F58", cursor:"pointer", userSelect:"none", marginBottom:6 }}>
                    Pipeline logs ({workflowRun.logs?.length || 0})
                  </summary>
                  <div style={{ background:"#0D1117", border:"1px solid #21262D", borderRadius:6, padding:"8px 10px", maxHeight:300, overflowY:"auto", marginTop:6 }}>
                    {(workflowRun.logs||[]).map((log,i) => (
                      <div key={i} style={{ fontSize:10.5, fontFamily:"monospace", lineHeight:1.6, color: log.level==="error" ? "#F85149" : log.level==="warning" ? "#FFA657" : "#8B949E" }}>
                        <span style={{ color:"#484F58" }}>[{log.stage?.replace("_"," ")}]</span>{" "}
                        {log.level==="error" ? "✗" : log.level==="warning" ? "⚠" : "→"} {log.message}
                      </div>
                    ))}
                  </div>
                </details>
              </div>
            )}
          </>
        )}
        {activeTab==="gcp_scan" && <GCPServiceScanPanel scan={scanResult} loading={scanLoading} onScan={handleScanGCP} />}
      </div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════════════════════
//  CURATION PANEL
// ══════════════════════════════════════════════════════════════════════════════

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  };
  return (
    <button onClick={copy} style={{ background:"none", border:"1px solid #21262D", borderRadius:4,
      color: copied ? "#3FB950" : "#484F58", fontSize:10, padding:"2px 8px", cursor:"pointer", fontFamily:"inherit" }}>
      {copied ? "✓ Copied" : "Copy"}
    </button>
  );
}

function fileIcon(filename) {
  const base = filename.split("/").pop();
  if (base === "README.md") return "📖";
  if (base === "main.tf") return "🏗";
  if (base === "variables.tf") return "📋";
  if (base === "outputs.tf") return "📤";
  if (base === "versions.tf") return "📌";
  if (base.endsWith(".tfvars.example") || base.endsWith(".tfvars")) return "⚙️";
  if (base.endsWith(".tf")) return "🔧";
  if (base.endsWith(".md")) return "📄";
  return "📄";
}

function fileTabLabel(filename) {
  // Shorten long subdirectory paths for display in tabs
  if (filename === "examples/complete/main.tf") return "examples/main.tf";
  if (filename === "terraform.tfvars.example") return ".tfvars.example";
  return filename;
}

function MarkdownView({ content }) {
  return (
    <div style={{ padding:"16px 20px", maxWidth:720 }}>
      {content.split("\n").map((line, i) => {
        if (line.startsWith("# "))
          return <h1 key={i} style={{ fontSize:16, color:"#E6EDF3", borderBottom:"1px solid #21262D", paddingBottom:8, marginBottom:12, fontFamily:"inherit" }}>{line.slice(2)}</h1>;
        if (line.startsWith("## "))
          return <h2 key={i} style={{ fontSize:13, color:"#C9D1D9", marginTop:16, marginBottom:6, fontFamily:"inherit" }}>{line.slice(3)}</h2>;
        if (line.startsWith("### "))
          return <h3 key={i} style={{ fontSize:12, color:"#8B949E", marginTop:10, marginBottom:4, fontFamily:"inherit" }}>{line.slice(4)}</h3>;
        if (line.startsWith("| "))
          return <div key={i} style={{ fontFamily:"monospace", fontSize:11, color:"#8B949E", lineHeight:1.8, borderBottom:"1px solid #21262D11" }}>{line}</div>;
        if (line.startsWith("```"))
          return <div key={i} style={{ height:4 }} />;
        if (line.startsWith("- ") || line.startsWith("* "))
          return <div key={i} style={{ fontSize:12, color:"#C9D1D9", lineHeight:1.7, paddingLeft:12, display:"flex", gap:6 }}><span style={{ color:"#484F58" }}>•</span>{line.slice(2)}</div>;
        if (line.trim() === "")
          return <div key={i} style={{ height:8 }} />;
        return <div key={i} style={{ fontSize:12, color:"#C9D1D9", lineHeight:1.7 }}>{line}</div>;
      })}
    </div>
  );
}

function HighlightedCode({ content, filename = "" }) {
  const base = filename.split("/").pop();
  if (base === "README.md") return <MarkdownView content={content} />;
  if (base.endsWith(".tfvars.example") || base.endsWith(".tfvars")) {
    return (
      <pre style={{ margin:0, padding:"12px 16px", fontSize:11.5, lineHeight:1.7, fontFamily:"monospace", whiteSpace:"pre-wrap", wordBreak:"break-word" }}>
        {content.split("\n").map((line, i) => {
          const isComment = line.trim().startsWith("#");
          return <span key={i} style={{ display:"block", color: isComment ? "#484F58" : "#C9D1D9" }}>{line}</span>;
        })}
      </pre>
    );
  }
  return (
    <pre style={{ margin:0, padding:"12px 16px", fontSize:11.5, lineHeight:1.7,
      fontFamily:"monospace", whiteSpace:"pre-wrap", wordBreak:"break-word" }}>
      {content.split("\n").map((line, i) => {
        const isSecLine  = line.includes("⚠️ SECURITY:");
        const isCostLine = line.includes("💰 COST:");
        const isTfLine   = line.includes("🔧 TFLINT:");
        const isComment  = line.trim().startsWith("#") && !isSecLine && !isCostLine && !isTfLine;
        const isKeyword  = /^\s*(resource|variable|output|locals|terraform|data|module|provider)\b/.test(line);
        const color = isSecLine ? "#F85149" : isCostLine ? "#FFA657" : isTfLine ? "#58A6FF"
          : isComment ? "#484F58" : isKeyword ? "#D2A8FF" : "#C9D1D9";
        const bg = isSecLine ? "#F8514910" : isCostLine ? "#FFA65710" : isTfLine ? "#58A6FF10" : "transparent";
        return <span key={i} style={{ display:"block", background:bg, color }}>{line}</span>;
      })}
    </pre>
  );
}

const SEV_COLOR = { error:"#F85149", warning:"#FFA657", info:"#58A6FF" };
const SEV_ICON  = { error:"✗", warning:"⚠", info:"ℹ" };
const RULE_ICON = { security_flag:"⚠️", cost_flag:"💰", tflint_flag:"🔧",
  open_ingress:"🔒", hardcoded_secret:"🔑", unknown_resource_type:"❌", unknown_attribute:"🔍",
  hcl_syntax:"📄", terraform_validate:"🔨", terraform_init:"🚀" };

function ValidationPanel({ validation }) {
  const [expanded, setExpanded] = useState(true);
  if (!validation) return null;

  const { passed, error_count, warning_count, issues = [],
    terraform_cli_available, terraform_validate_passed, provider_schema_checked } = validation;

  const secFlags  = issues.filter(i => i.rule === "security_flag");
  const costFlags = issues.filter(i => i.rule === "cost_flag");
  const errors    = issues.filter(i => i.severity === "error" && i.rule !== "security_flag");
  const warnings  = issues.filter(i => i.severity === "warning" && i.rule !== "security_flag" && i.rule !== "cost_flag");

  return (
    <div style={{ borderTop:"1px solid #21262D", background:"#0D1117", flexShrink:0 }}>
      {/* Summary bar */}
      <div onClick={() => setExpanded(e => !e)} style={{
        display:"flex", alignItems:"center", gap:8, padding:"7px 14px", cursor:"pointer",
        borderBottom: expanded && issues.length ? "1px solid #21262D" : "none",
      }}>
        <span style={{ fontSize:11, fontWeight:700, color: passed ? "#3FB950" : "#F85149" }}>
          {passed ? "✅ Validation passed" : "❌ Validation issues"}
        </span>
        {error_count > 0 && (
          <span style={{ fontSize:10, background:"#F8514922", color:"#F85149",
            border:"1px solid #F8514944", borderRadius:4, padding:"1px 6px" }}>
            {error_count} error{error_count>1?"s":""}
          </span>
        )}
        {warning_count > 0 && (
          <span style={{ fontSize:10, background:"#FFA65722", color:"#FFA657",
            border:"1px solid #FFA65744", borderRadius:4, padding:"1px 6px" }}>
            {warning_count} warning{warning_count>1?"s":""}
          </span>
        )}
        {secFlags.length > 0 && (
          <span style={{ fontSize:10, background:"#F8514911", color:"#F85149",
            border:"1px solid #F8514933", borderRadius:4, padding:"1px 6px" }}>
            ⚠️ {secFlags.length} security
          </span>
        )}
        {costFlags.length > 0 && (
          <span style={{ fontSize:10, background:"#FFA65711", color:"#FFA657",
            border:"1px solid #FFA65733", borderRadius:4, padding:"1px 6px" }}>
            💰 {costFlags.length} cost
          </span>
        )}
        <span style={{ marginLeft:"auto", display:"flex", gap:10, alignItems:"center" }}>
          {terraform_cli_available && (
            <span style={{ fontSize:10, color: terraform_validate_passed ? "#3FB950" : "#F85149" }}>
              {terraform_validate_passed ? "✓" : "✗"} terraform validate
            </span>
          )}
          {provider_schema_checked && (
            <span style={{ fontSize:10, color:"#58A6FF" }}>✓ schema</span>
          )}
          <span style={{ fontSize:10, color:"#484F58" }}>{expanded ? "▲" : "▼"}</span>
        </span>
      </div>

      {/* Issue list */}
      {expanded && issues.length > 0 && (
        <div style={{ maxHeight:180, overflowY:"auto", padding:"6px 0" }}>
          {issues.map((issue, idx) => (
            <div key={idx} style={{
              display:"flex", gap:8, padding:"4px 14px", alignItems:"flex-start",
              borderBottom:"1px solid #21262D22",
            }}>
              <span style={{ fontSize:11, color: SEV_COLOR[issue.severity] || "#8B949E",
                flexShrink:0, lineHeight:1.6 }}>
                {RULE_ICON[issue.rule] || SEV_ICON[issue.severity] || "•"}
              </span>
              <div style={{ flex:1, minWidth:0 }}>
                <div style={{ display:"flex", gap:6, alignItems:"center", flexWrap:"wrap" }}>
                  <span style={{ fontSize:10, fontFamily:"monospace",
                    color: SEV_COLOR[issue.severity] || "#8B949E" }}>
                    [{issue.rule}]
                  </span>
                  {issue.file && (
                    <span style={{ fontSize:10, color:"#484F58", fontFamily:"monospace" }}>
                      {issue.file}{issue.line ? `:${issue.line}` : ""}
                    </span>
                  )}
                </div>
                <div style={{ fontSize:11.5, color:"#C9D1D9", lineHeight:1.5 }}>{issue.message}</div>
                {issue.suggestion && (
                  <div style={{ fontSize:10.5, color:"#8B949E", lineHeight:1.4, marginTop:1 }}>
                    💡 {issue.suggestion}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CodeViewer({ files, outputDir, summary, usageExample, gitTag, validation }) {
  const [selected, setSelected] = useState(files[0]?.filename || "");
  const activeFile = files.find(f => f.filename === selected);

  // File-specific issues for the active tab badge
  const fileIssueCount = (validation?.issues || []).filter(
    i => i.file === selected && i.severity === "error"
  ).length;

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      {/* Summary bar */}
      <div style={{ padding:"10px 16px", background:"#0D1117", borderBottom:"1px solid #21262D", flexShrink:0 }}>
        <div style={{ fontSize:12, color:"#3FB950", marginBottom:4, fontWeight:700 }}>✅ Code Generated Successfully</div>
        {summary && <div style={{ fontSize:11.5, color:"#8B949E", lineHeight:1.5, marginBottom:6 }}>{summary}</div>}
        <div style={{ display:"flex", gap:10, alignItems:"center", flexWrap:"wrap" }}>
          <div style={{ fontFamily:"monospace", fontSize:10.5, color:"#58A6FF", background:"#1F6FEB11",
            border:"1px solid #1F6FEB33", borderRadius:4, padding:"3px 8px", maxWidth:"100%", wordBreak:"break-all" }}>
            📁 {outputDir}
          </div>
          {gitTag && <Badge label={`git tag: ${gitTag}`} color="#3FB950" />}
        </div>
      </div>

      {/* File tabs */}
      <div style={{ display:"flex", borderBottom:"1px solid #21262D", background:"#161B22", flexShrink:0, overflowX:"auto" }}>
        {files.map(f => {
          const errCount = (validation?.issues || []).filter(
            i => i.file === f.filename && i.severity === "error"
          ).length;
          return (
            <button key={f.filename} onClick={() => setSelected(f.filename)}
              title={f.filename}
              style={{
                background:"none", border:"none", cursor:"pointer", padding:"7px 12px", fontSize:11,
                fontFamily:"monospace", fontWeight: selected===f.filename ? 600 : 400,
                color: selected===f.filename ? "#E6EDF3" : "#484F58",
                borderBottom:`2px solid ${selected===f.filename ? "#58A6FF" : "transparent"}`,
                whiteSpace:"nowrap", display:"flex", alignItems:"center", gap:4,
              }}>
              <span style={{ fontSize:12 }}>{fileIcon(f.filename)}</span>
              {fileTabLabel(f.filename)}
              {errCount > 0 && (
                <span style={{ fontSize:9, background:"#F85149", color:"#fff",
                  borderRadius:8, padding:"0 4px", lineHeight:"14px" }}>{errCount}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* File content */}
      <div style={{ flex:1, overflowY:"auto", position:"relative" }}>
        {activeFile && (
          <>
            <div style={{ position:"sticky", top:0, right:0, display:"flex", justifyContent:"flex-end",
              padding:"6px 12px", background:"#0D1117", borderBottom:"1px solid #21262D", zIndex:1 }}>
              <CopyButton text={activeFile.content} />
            </div>
            <HighlightedCode content={activeFile.content} filename={activeFile.filename} />
          </>
        )}
      </div>

      {/* Validation panel */}
      <ValidationPanel validation={validation} />

      {/* Usage example */}
      {usageExample && (
        <div style={{ borderTop:"1px solid #21262D", padding:"10px 16px", flexShrink:0, background:"#0D1117" }}>
          <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:6 }}>
            <div style={{ fontSize:10, color:"#484F58", textTransform:"uppercase", letterSpacing:"0.06em" }}>Usage Example</div>
            <CopyButton text={usageExample} />
          </div>
          <pre style={{ margin:0, fontSize:11, lineHeight:1.6, color:"#D2A8FF", fontFamily:"monospace",
            whiteSpace:"pre-wrap", wordBreak:"break-word", background:"#161B22",
            border:"1px solid #21262D", borderRadius:6, padding:"8px 12px" }}>
            {usageExample}
          </pre>
        </div>
      )}
    </div>
  );
}

function CurationChat({ session, onAnswer, onGenerate, generating }) {
  const [answerInput, setAnswerInput] = useState("");
  const bottomRef = useRef(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior:"smooth" }); }, [session]);

  if (!session) return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
      height:"100%", gap:10, opacity:0.4 }}>
      <div style={{ fontSize:28 }}>🔧</div>
      <div style={{ fontSize:12, color:"#8B949E" }}>Configure a session to start curation</div>
    </div>
  );

  const provColor = PROVIDER_COLORS[session.provider] || "#484F58";
  const isDone = session.status === "done";
  const isError = session.status === "error";
  const isReady = session.status === "ready";
  const isAsking = session.status === "asking";
  const isGenerating = session.status === "generating" || generating;

  const handleAnswer = () => {
    if (!answerInput.trim()) return;
    onAnswer(answerInput.trim());
    setAnswerInput("");
  };

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      {/* Session header */}
      <div style={{ padding:"10px 16px", borderBottom:"1px solid #21262D", background:"#161B22", flexShrink:0,
        display:"flex", alignItems:"center", gap:10, flexWrap:"wrap" }}>
        <Badge label={PROVIDER_LABELS[session.provider] || session.provider} color={provColor} />
        <Badge label={MODE_LABELS[session.mode] || session.mode} color="#8B949E" />
        {session.service_name && <span style={{ fontSize:11.5, color:"#E6EDF3", fontWeight:600 }}>{session.service_name}</span>}
        <span style={{ marginLeft:"auto" }}>
          <StatusDot status={session.status} />
          <span style={{ fontSize:10, color:"#484F58", marginLeft:5 }}>{session.status}</span>
        </span>
      </div>

      {/* Q&A area */}
      <div style={{ flex:1, overflowY:"auto", padding:"16px 20px" }}>
        {/* Progress */}
        {session.questions.length > 0 && (
          <div style={{ marginBottom:14 }}>
            <div style={{ display:"flex", gap:4 }}>
              {session.questions.map((_, i) => (
                <div key={i} style={{ flex:1, height:3, borderRadius:2,
                  background: i < session.current_question_idx ? "#3FB950"
                    : i === session.current_question_idx ? "#FFA657"
                    : "#21262D" }} />
              ))}
            </div>
            <div style={{ fontSize:10, color:"#484F58", marginTop:4 }}>
              {session.all_questions_answered ? "All questions answered" : `Question ${session.current_question_idx + 1} of ${session.questions.length}`}
            </div>
          </div>
        )}

        {/* Answered pairs */}
        {session.qa_pairs.map((qa, i) => (
          <div key={i} style={{ marginBottom:14 }}>
            <div style={{ display:"flex", gap:8, marginBottom:6 }}>
              <div style={{ width:22, height:22, borderRadius:5,
                background:"linear-gradient(135deg,#1F6FEB,#3FB950)",
                display:"flex", alignItems:"center", justifyContent:"center",
                fontSize:11, flexShrink:0 }}>🤖</div>
              <div style={{ background:"#161B22", border:"1px solid #21262D",
                borderRadius:"2px 12px 12px 12px", padding:"8px 12px", flex:1 }}>
                <div style={{ fontSize:12.5, color:"#C9D1D9", lineHeight:1.6 }}>{qa.question}</div>
              </div>
            </div>
            <div style={{ display:"flex", justifyContent:"flex-end" }}>
              <div style={{ background:"#1F6FEB22", border:"1px solid #1F6FEB44",
                borderRadius:"12px 12px 2px 12px", padding:"8px 12px", maxWidth:"80%" }}>
                <div style={{ fontSize:12.5, color:"#E6EDF3", lineHeight:1.5 }}>{qa.answer}</div>
              </div>
            </div>
          </div>
        ))}

        {/* Current question */}
        {isAsking && session.current_question && (
          <div style={{ display:"flex", gap:8, marginBottom:6 }}>
            <div style={{ width:22, height:22, borderRadius:5,
              background:"linear-gradient(135deg,#1F6FEB,#3FB950)",
              display:"flex", alignItems:"center", justifyContent:"center", fontSize:11, flexShrink:0 }}>🤖</div>
            <div style={{ background:"#161B22", border:"1px solid #58A6FF44",
              borderRadius:"2px 12px 12px 12px", padding:"8px 12px", flex:1 }}>
              <div style={{ fontSize:12.5, color:"#E6EDF3", lineHeight:1.6 }}>{session.current_question}</div>
            </div>
          </div>
        )}

        {/* Generating spinner — 3-pass progress */}
        {isGenerating && (
          <div style={{ background:"#161B22", border:"1px solid #FFA65733", borderRadius:8, padding:"14px 16px", marginTop:8 }}>
            <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:12 }}>
              <div style={{ display:"flex", gap:4 }}>
                {[0,1,2].map(i => <div key={i} style={{ width:6, height:6, borderRadius:"50%", background:"#FFA657", animation:`blink 1.2s ${i*0.2}s ease-in-out infinite` }} />)}
              </div>
              <span style={{ fontSize:12, color:"#FFA657", fontWeight:600 }}>Generating Terraform module…</span>
            </div>
            {[
              { label:"Pass A", desc:"main.tf — resources, locals, data sources", icon:"🏗" },
              { label:"Pass B", desc:"variables.tf + outputs.tf", icon:"📋" },
              { label:"Pass C", desc:"versions.tf · README.md · examples · .tfvars", icon:"📦" },
            ].map((p, i) => (
              <div key={i} style={{ display:"flex", alignItems:"center", gap:10, padding:"5px 0",
                opacity: 0.4 + i * 0.1 }}>
                <span style={{ fontSize:13 }}>{p.icon}</span>
                <div>
                  <span style={{ fontSize:11, color:"#FFA657", fontWeight:600 }}>{p.label}</span>
                  <span style={{ fontSize:10.5, color:"#484F58", marginLeft:6 }}>{p.desc}</span>
                </div>
              </div>
            ))}
            <div style={{ fontSize:10, color:"#484F58", marginTop:10 }}>
              3 LLM passes — typically 45–120 seconds with gemma3:4b
            </div>
          </div>
        )}

        {/* Error */}
        {isError && session.error && (
          <div style={{ background:"#F8514911", border:"1px solid #F8514933", borderRadius:8,
            padding:"12px 16px", marginTop:8 }}>
            <div style={{ fontSize:12, color:"#F85149" }}>⚠ Generation error</div>
            <div style={{ fontSize:11, color:"#8B949E", marginTop:4 }}>{session.error}</div>
          </div>
        )}

        {/* Registry docs badge */}
        {session.registry_docs_available && (
          <div style={{ fontSize:10, color:"#3FB950", background:"#3FB95011", border:"1px solid #3FB95033",
            borderRadius:4, padding:"2px 8px", display:"inline-block", marginBottom:8 }}>
            ✓ Provider docs loaded
          </div>
        )}

        {/* Module files badge */}
        {session.tf_files_loaded?.length > 0 && (
          <div style={{ fontSize:10, color:"#58A6FF", background:"#58A6FF11", border:"1px solid #58A6FF33",
            borderRadius:4, padding:"2px 8px", display:"inline-block", marginBottom:8, marginLeft:6 }}>
            ✓ {session.tf_files_loaded.length} .tf files loaded
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      {(isAsking || isReady || isDone) && (
        <div style={{ borderTop:"1px solid #21262D", padding:"12px 16px", background:"#0D1117", flexShrink:0 }}>
          {isAsking && session.current_question && (
            <div style={{ display:"flex", gap:8, alignItems:"flex-end" }}>
              <textarea
                value={answerInput}
                onChange={e => setAnswerInput(e.target.value)}
                onKeyDown={e => { if (e.key==="Enter" && !e.shiftKey) { e.preventDefault(); handleAnswer(); } }}
                placeholder="Type your answer… (Enter to submit)"
                rows={2}
                style={{ flex:1, background:"#161B22", border:"1px solid #21262D", borderRadius:6,
                  color:"#E6EDF3", fontSize:12, fontFamily:"inherit", resize:"none", padding:"8px 12px",
                  lineHeight:1.5, maxHeight:100, overflowY:"auto" }}
              />
              <button onClick={handleAnswer} disabled={!answerInput.trim()} style={{
                width:36, height:36, borderRadius:6, background:"#1F6FEB",
                border:"none", cursor:"pointer", display:"flex", alignItems:"center",
                justifyContent:"center", fontSize:14, flexShrink:0, opacity: answerInput.trim() ? 1 : 0.4 }}>
                ↑
              </button>
            </div>
          )}
          {(isReady || (isDone && !session.result)) && !isGenerating && (
            <button onClick={onGenerate} disabled={isGenerating} style={{
              width:"100%", background:"#3FB95022", border:"1px solid #3FB95066",
              borderRadius:8, color:"#3FB950", fontSize:13, fontWeight:700,
              padding:"10px 0", cursor:"pointer", fontFamily:"inherit",
              display:"flex", alignItems:"center", justifyContent:"center", gap:8 }}>
              ⚡ Generate Terraform Code
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function CurationPanel({ repos, health }) {
  const [mode, setMode] = useState("new_product");
  const [provider, setProvider] = useState("google");
  const [serviceName, setServiceName] = useState("");
  const [repoName, setRepoName] = useState("");
  const [newTag, setNewTag] = useState("");
  const [description, setDescription] = useState("");
  const [sourceType, setSourceType] = useState("none");
  const [githubUrl, setGithubUrl] = useState("");
  const [githubTag, setGithubTag] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [session, setSession] = useState(null);
  const [starting, setStarting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [sourceReady, setSourceReady] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState(null);
  const docFileRef = useRef(null);
  const moduleFileRef = useRef(null);
  const ollamaOk = health?.ollama === "running";

  const resetSession = () => {
    setSession(null); setSourceReady(false); setError(null);
    setStarting(false); setUploading(false); setGenerating(false);
  };

  const handleStart = async () => {
    if (!ollamaOk) { setError("Ollama is not running"); return; }
    if (mode === "new_product" && !serviceName.trim()) { setError("Enter a service name"); return; }
    if (mode === "self_curation" && (!repoName || !newTag.trim())) { setError("Select a repo and enter a new tag name"); return; }

    setStarting(true); setError(null);
    try {
      const view = await apiPost("/curate/start", {
        mode, provider, service_name: serviceName.trim(),
        repo_name: repoName || null, new_tag: newTag.trim() || null,
        description: description.trim(),
      });
      setSession(view);
      setSourceReady(mode === "new_product" || mode === "self_curation");
    } catch (e) { setError(e.message); }
    finally { setStarting(false); }
  };

  const handleDocUpload = async (file) => {
    if (!session || !file) return;
    setUploading(true); setError(null);
    const fd = new FormData(); fd.append("file", file);
    try {
      const view = await apiUpload(`/curate/${session.session_id}/upload-doc`, fd);
      setSession(view); setSourceReady(true);
    } catch (e) { setError(e.message); }
    finally { setUploading(false); }
  };

  const handleModuleUpload = async (file) => {
    if (!session || !file) return;
    setUploading(true); setError(null);
    const fd = new FormData(); fd.append("file", file);
    try {
      const view = await apiUpload(`/curate/${session.session_id}/upload-module`, fd);
      setSession(view); setSourceReady(true);
    } catch (e) { setError(e.message); }
    finally { setUploading(false); }
  };

  const handleSetSource = async (type) => {
    if (!session) return;
    setUploading(true); setError(null);
    try {
      const body = { source_type: type };
      if (type === "github") { body.url = githubUrl; body.tag = githubTag || null; }
      if (type === "local")  { body.path = localPath; }
      const view = await apiPost(`/curate/${session.session_id}/set-source`, body);
      setSession(view); setSourceReady(true);
    } catch (e) { setError(e.message); }
    finally { setUploading(false); }
  };

  const handleAnswer = async (answer) => {
    if (!session) return;
    try {
      const view = await apiPost(`/curate/${session.session_id}/answer`, { answer });
      setSession(view);
    } catch (e) { setError(e.message); }
  };

  const handleGenerate = async () => {
    if (!session) return;
    setGenerating(true); setError(null);
    try {
      // Fire generation (long-running — 3 LLM passes)
      apiPost(`/curate/${session.session_id}/generate`, {}).catch(() => {});
      // Poll until done or error (avoids browser fetch timeout on slow hardware)
      const poll = async () => {
        for (let i = 0; i < 120; i++) {
          await new Promise(r => setTimeout(r, 3000));
          try {
            const view = await apiGet(`/curate/${session.session_id}`);
            setSession(view);
            if (view.status === "done" || view.status === "error") return;
          } catch (_) {}
        }
        setError("Generation timed out — check the backend logs.");
      };
      await poll();
    } catch (e) { setError(e.message); }
    finally { setGenerating(false); }
  };

  const isDone = session?.status === "done";
  const hasResult = isDone && session?.result?.files?.length > 0;

  const inputStyle = {
    background:"#161B22", border:"1px solid #21262D", borderRadius:5,
    color:"#E6EDF3", fontSize:11.5, padding:"6px 10px", fontFamily:"inherit",
    width:"100%",
  };
  const labelStyle = { fontSize:10, color:"#484F58", textTransform:"uppercase",
    letterSpacing:"0.06em", marginBottom:4, display:"block" };
  const sectionStyle = { marginBottom:14 };

  return (
    <div style={{ display:"flex", height:"100%", overflow:"hidden" }}>

      {/* ── Left config panel ── */}
      <div style={{ width:280, borderRight:"1px solid #21262D", display:"flex",
        flexDirection:"column", background:"#161B22", flexShrink:0, overflowY:"auto" }}>
        <div style={{ padding:"12px 14px", borderBottom:"1px solid #21262D" }}>
          <div style={{ fontSize:12.5, fontWeight:700, color:"#E6EDF3", marginBottom:2 }}>🔧 Module Curation</div>
          <div style={{ fontSize:10.5, color:"#484F58", marginBottom:6 }}>Generate complete Terraform modules</div>
          <div style={{ display:"flex", gap:6, flexWrap:"wrap" }}>
            <span style={{ fontSize:9, color:"#3FB950", background:"#3FB95011", border:"1px solid #3FB95033", borderRadius:3, padding:"1px 6px" }}>7 files</span>
            <span style={{ fontSize:9, color:"#58A6FF", background:"#58A6FF11", border:"1px solid #58A6FF33", borderRadius:3, padding:"1px 6px" }}>GCP · AWS · Azure</span>
            {health?.network_available === false && (
              <span style={{ fontSize:9, color:"#FFA657", background:"#FFA65711", border:"1px solid #FFA65733", borderRadius:3, padding:"1px 6px" }}>📡 Offline — using cache</span>
            )}
          </div>
        </div>

        <div style={{ padding:"14px", flex:1 }}>
          {/* Mode */}
          <div style={sectionStyle}>
            <label style={labelStyle}>Curation Mode</label>
            <select value={mode} onChange={e => { setMode(e.target.value); resetSession(); }}
              style={{ ...inputStyle }}>
              <option value="new_product">New Product (from service name)</option>
              <option value="from_document">From Document (PDF / DOCX / TXT)</option>
              <option value="from_module">From Module (GitHub / local / ZIP)</option>
              <option value="self_curation">Self-Curation (modify + new tag)</option>
            </select>
          </div>

          {/* Provider */}
          <div style={sectionStyle}>
            <label style={labelStyle}>Cloud Provider</label>
            <div style={{ display:"flex", gap:6 }}>
              {[{v:"google",l:"GCP"},{v:"aws",l:"AWS"},{v:"azurerm",l:"Azure"}].map(p => (
                <button key={p.v} onClick={() => setProvider(p.v)} style={{
                  flex:1, background: provider===p.v ? PROVIDER_COLORS[p.v]+"22" : "#0D1117",
                  border:`1px solid ${provider===p.v ? PROVIDER_COLORS[p.v]+"66" : "#21262D"}`,
                  borderRadius:5, color: provider===p.v ? PROVIDER_COLORS[p.v] : "#484F58",
                  fontSize:11, padding:"5px 0", cursor:"pointer", fontFamily:"inherit", fontWeight: provider===p.v ? 700 : 400,
                }}>{p.l}</button>
              ))}
            </div>
          </div>

          {/* Service name (always shown except self_curation where repo covers it) */}
          {mode !== "self_curation" && (
            <div style={sectionStyle}>
              <label style={labelStyle}>Service / Product Name</label>
              <input value={serviceName} onChange={e => setServiceName(e.target.value)}
                placeholder="e.g. Cloud Run, S3, Azure Functions"
                style={inputStyle} />
            </div>
          )}

          {/* Self-curation extras */}
          {mode === "self_curation" && (
            <>
              <div style={sectionStyle}>
                <label style={labelStyle}>Existing Repo</label>
                <select value={repoName} onChange={e => setRepoName(e.target.value)} style={inputStyle}>
                  <option value="">— Select a repo —</option>
                  {repos.map(r => <option key={r.name} value={r.name}>{r.display_name} ({r.name})</option>)}
                </select>
              </div>
              <div style={sectionStyle}>
                <label style={labelStyle}>New Tag Name</label>
                <input value={newTag} onChange={e => setNewTag(e.target.value)}
                  placeholder="e.g. v2.1.0"
                  style={{ ...inputStyle, fontFamily:"monospace" }} />
              </div>
            </>
          )}

          {/* Description / seed text */}
          <div style={sectionStyle}>
            <label style={labelStyle}>Initial Description (optional)</label>
            <textarea value={description} onChange={e => setDescription(e.target.value)}
              placeholder="Briefly describe what you want to build or change…"
              rows={3}
              style={{ ...inputStyle, resize:"vertical" }} />
          </div>

          {/* Start button */}
          {!session && (
            <button onClick={handleStart} disabled={starting || !ollamaOk}
              style={{ width:"100%", background: ollamaOk ? "#1F6FEB22" : "#21262D",
                border:`1px solid ${ollamaOk ? "#1F6FEB66" : "#21262D"}`, borderRadius:6,
                color: ollamaOk ? "#58A6FF" : "#484F58", fontSize:12, fontWeight:600,
                padding:"8px 0", cursor: ollamaOk ? "pointer" : "not-allowed", fontFamily:"inherit" }}>
              {starting ? "Starting…" : "Start Session →"}
            </button>
          )}

          {/* Source input (shown after session started, for from_document / from_module) */}
          {session && !sourceReady && (
            <div style={{ marginTop:14, padding:"12px", background:"#0D1117",
              border:"1px solid #21262D", borderRadius:8 }}>
              <div style={{ fontSize:10, color:"#FFA657", textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:10 }}>
                Provide Source
              </div>

              {mode === "from_document" && (
                <>
                  <button onClick={() => docFileRef.current?.click()} disabled={uploading}
                    style={{ ...inputStyle, cursor:"pointer", textAlign:"center", color:"#58A6FF",
                      border:"1px dashed #1F6FEB66", padding:"10px", marginBottom:8 }}>
                    {uploading ? "Uploading…" : "📄 Upload PDF / DOCX / TXT"}
                  </button>
                  <input ref={docFileRef} type="file" accept=".pdf,.docx,.doc,.txt,.md"
                    style={{ display:"none" }}
                    onChange={e => e.target.files?.[0] && handleDocUpload(e.target.files[0])} />
                </>
              )}

              {mode === "from_module" && (
                <>
                  {/* Source type selector */}
                  <div style={{ display:"flex", gap:4, marginBottom:10, flexWrap:"wrap" }}>
                    {[{v:"github",l:"GitHub"},{v:"local",l:"Local Path"},{v:"zip",l:"ZIP Upload"},{v:"tf_upload",l:".tf Files"}].map(s => (
                      <button key={s.v} onClick={() => setSourceType(s.v)} style={{
                        background: sourceType===s.v ? "#1F6FEB22" : "#161B22",
                        border:`1px solid ${sourceType===s.v ? "#1F6FEB66" : "#21262D"}`,
                        borderRadius:4, color: sourceType===s.v ? "#58A6FF" : "#484F58",
                        fontSize:10, padding:"4px 8px", cursor:"pointer", fontFamily:"inherit" }}>
                        {s.l}
                      </button>
                    ))}
                  </div>

                  {sourceType === "github" && (
                    <>
                      <input value={githubUrl} onChange={e => setGithubUrl(e.target.value)}
                        placeholder="https://github.com/org/repo" style={{ ...inputStyle, marginBottom:6 }} />
                      <input value={githubTag} onChange={e => setGithubTag(e.target.value)}
                        placeholder="Tag/branch (optional)" style={{ ...inputStyle, marginBottom:8 }} />
                      <button onClick={() => handleSetSource("github")} disabled={uploading || !githubUrl.trim()}
                        style={{ ...inputStyle, cursor:"pointer", textAlign:"center", color:"#58A6FF", border:"1px solid #1F6FEB44" }}>
                        {uploading ? "Cloning…" : "Clone & Load"}
                      </button>
                    </>
                  )}

                  {sourceType === "local" && (
                    <>
                      <input value={localPath} onChange={e => setLocalPath(e.target.value)}
                        placeholder="C:\path\to\module or /home/..." style={{ ...inputStyle, marginBottom:8, fontFamily:"monospace" }} />
                      <button onClick={() => handleSetSource("local")} disabled={uploading || !localPath.trim()}
                        style={{ ...inputStyle, cursor:"pointer", textAlign:"center", color:"#58A6FF", border:"1px solid #1F6FEB44" }}>
                        {uploading ? "Loading…" : "Load Local Module"}
                      </button>
                    </>
                  )}

                  {(sourceType === "zip" || sourceType === "tf_upload") && (
                    <>
                      <button onClick={() => moduleFileRef.current?.click()} disabled={uploading}
                        style={{ ...inputStyle, cursor:"pointer", textAlign:"center", color:"#58A6FF",
                          border:"1px dashed #1F6FEB66", padding:"10px" }}>
                        {uploading ? "Uploading…" : sourceType === "zip" ? "📦 Upload ZIP" : "📄 Upload .tf File"}
                      </button>
                      <input ref={moduleFileRef} type="file"
                        accept={sourceType === "zip" ? ".zip" : ".tf"}
                        style={{ display:"none" }}
                        onChange={e => e.target.files?.[0] && handleModuleUpload(e.target.files[0])} />
                    </>
                  )}
                </>
              )}
            </div>
          )}

          {/* Session reset */}
          {session && (
            <button onClick={resetSession} style={{ marginTop:10, width:"100%", background:"none",
              border:"1px solid #21262D", borderRadius:5, color:"#484F58", fontSize:10,
              padding:"5px 0", cursor:"pointer", fontFamily:"inherit" }}>
              ↩ Reset Session
            </button>
          )}

          {/* Error */}
          {error && (
            <div style={{ marginTop:10, background:"#F8514911", border:"1px solid #F8514933",
              borderRadius:6, padding:"8px 10px", fontSize:11, color:"#F85149", lineHeight:1.5 }}>
              {error}
            </div>
          )}
        </div>
      </div>

      {/* ── Right panel: Q&A or code viewer ── */}
      <div style={{ flex:1, overflow:"hidden", display:"flex", flexDirection:"column" }}>
        {hasResult ? (
          <CodeViewer
            files={session.result.files}
            outputDir={session.result.output_dir}
            summary={session.result.summary}
            usageExample={session.result.usage_example}
            gitTag={session.result.git_tag_created ? session.result.git_tag_name : null}
            validation={session.result.validation ?? null}
          />
        ) : (
          <CurationChat
            session={session}
            onAnswer={handleAnswer}
            onGenerate={handleGenerate}
            generating={generating || session?.status === "generating"}
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
  const [mainView, setMainView] = useState("chat");
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
        textarea:focus,input:focus,select:focus { outline:1px solid #1F6FEB44; }
        button:hover { opacity:.85; }
        select { appearance:none; }
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
            background:"#21262D", borderRadius:3 }}>v2.1</span>
        </div>

        {/* View toggle */}
        <div style={{ display:"flex", gap:2, background:"#0D1117",
          borderRadius:6, padding:3, border:"1px solid #21262D" }}>
          {[
            { key:"chat",   label:"💬 Chat" },
            { key:"curate", label:"🔧 Curate" },
            { key:"ga",     label:"🚀 GA Workflow" },
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

        {health?.network_available !== undefined && (
          <div style={{ fontSize:10, color: health.network_available ? "#3FB950" : "#FFA657",
            background: health.network_available ? "#3FB95011" : "#FFA65711",
            border:`1px solid ${health.network_available ? "#3FB95033" : "#FFA65733"}`,
            padding:"2px 8px", borderRadius:3 }}>
            {health.network_available ? "🌐 Online" : "📡 Offline"}
          </div>
        )}

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

        {/* ── Sidebar (only for chat + ga views) ── */}
        {mainView !== "curate" && (
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
        )}

        {/* ── Main area ── */}
        {mainView === "chat" && (
          <div style={{ flex:1, display:"flex", flexDirection:"column", overflow:"hidden" }}>
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
        )}

        {mainView === "curate" && (
          <CurationPanel repos={repos} health={health} />
        )}

        {mainView === "ga" && (
          <div style={{ flex:1, overflow:"hidden" }}>
            <GAWorkflowPanel repo={selectedRepo} health={health} />
          </div>
        )}
      </div>
    </div>
  );
}
