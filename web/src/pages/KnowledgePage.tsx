import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Database, KeyRound, Play, RefreshCw, ShieldCheck } from "lucide-react";
import { api, type KnowledgeSetupStatus } from "@/lib/api";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { usePageHeader } from "@/contexts/usePageHeader";

function actionKey(): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  return `knowledge-${random}`;
}

function StatusBadge({ status }: { status: KnowledgeSetupStatus | null }) {
  if (!status) return null;
  return <Badge tone={status.ok || status.status === "ready" ? "success" : status.status === "error" ? "destructive" : "outline"}>{status.ok || status.status === "ready" ? "Ready" : status.status === "error" ? "Needs attention" : "Setup needed"}</Badge>;
}

export default function KnowledgePage() {
  const { setTitle } = usePageHeader();
  useEffect(() => { setTitle("Memory & Knowledge"); return () => setTitle(null); }, [setTitle]);
  const [csrfToken, setCsrfToken] = useState("");
  const [status, setStatus] = useState<KnowledgeSetupStatus | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const result = await api.getKnowledgeSetup();
      setCsrfToken(result.csrf_token);
      setStatus(result.status);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load knowledge setup.");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const run = async (action: "save" | "start" | "check") => {
    if (!csrfToken) return;
    setBusy(action);
    setError("");
    try {
      const result = action === "save"
        ? await api.saveKnowledgeSetup(csrfToken, actionKey(), apiKey || undefined)
        : action === "start"
          ? await api.startKnowledgeSetup(csrfToken, actionKey())
          : await api.checkKnowledgeSetup(csrfToken, actionKey());
      setCsrfToken(result.csrf_token);
      setStatus(result.status);
      if (action === "save") setApiKey("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "The knowledge action failed.");
      void load();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-4 sm:p-6">
      <div>
        <h1 className="text-2xl font-semibold">Memory &amp; Knowledge</h1>
        <p className="mt-1 text-sm text-muted-foreground">Set up Frank’s single Hermes-owned knowledge brain. Secrets stay in Hermes and are never shown back.</p>
      </div>

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-3">
            <CardTitle className="flex items-center gap-2"><KeyRound className="h-4 w-4" /> OpenAI connection</CardTitle>
            <StatusBadge status={status} />
          </div>
          <CardDescription>Enter the API key once. Hermes stores only a protected, write-only value indicator.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="knowledge-openai-key">OpenAI API key</Label>
            <Input id="knowledge-openai-key" type="password" autoComplete="new-password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={status?.api_key_set ? "Already set — leave blank to keep it" : "Paste your API key"} />
          </div>
          <Button onClick={() => void run("save")} disabled={busy !== null || (!apiKey && !status?.api_key_set)} prefix={busy === "save" ? <Spinner /> : <ShieldCheck />}>Save securely</Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Database className="h-4 w-4" /> Frank knowledge boundary</CardTitle>
          <CardDescription>These values are fixed by Hermes so memory cannot cross projects or leak through Frank.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <div className="flex items-center justify-between gap-4"><span>Namespace</span><code>project/frank</code></div>
          <div className="flex items-center justify-between gap-4"><span>Allowed project</span><code>project/frank</code></div>
          <div className="flex items-center justify-between gap-4"><span>Recommended database</span><span>{status?.neo4j_version ?? "Neo4j 5.26 Community"} · immutable release</span></div>
          <div className="flex items-center justify-between gap-4"><span>Database image</span><Badge tone={status?.neo4j_image_pinned ? "success" : "outline"}>{status?.neo4j_image_pinned ? "Pinned" : "Will be pinned on save"}</Badge></div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Play className="h-4 w-4" /> Knowledge services</CardTitle>
          <CardDescription>Start and check the committed, allowlisted deployment. Hermes creates internal tokens and passwords without displaying them.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3">
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => void run("start")} disabled={busy !== null || !status?.configured} prefix={busy === "start" ? <Spinner /> : <Play />}>Save &amp; start</Button>
            <Button outlined onClick={() => void run("check")} disabled={busy !== null} prefix={busy === "check" ? <Spinner /> : <RefreshCw />}>Check / retry</Button>
          </div>
          <div aria-live="polite" className="flex items-center gap-2 text-sm text-muted-foreground"><CheckCircle2 className="h-4 w-4" />{status?.message ?? "Loading setup status…"}</div>
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        </CardContent>
      </Card>
    </div>
  );
}
