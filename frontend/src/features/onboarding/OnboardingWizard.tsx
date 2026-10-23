import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Checkbox, Descriptions, Skeleton, Space, Steps, Tag, Typography } from "antd";
import * as api from "../../api/client";
import type {
  ActivationView, CredentialReference, CredentialStatusView, DiagnosticsReport, DraftView,
  InitializationPreview, OnboardingReport, OnboardingView, OperationView, ProbeView,
  ProviderCatalogEntry, ReadinessReport, RestartView, SettingsView, ValidationReport,
} from "../../api/contracts";
import { useSettingsSession } from "../../auth/SettingsSession";
import { useSessionResource } from "../../hooks/useApiResource";
import { BusinessStep } from "./BusinessStep";
import { referenceFromStatus, type CredentialPurpose } from "./CredentialInput";
import { ModelStep } from "./ModelStep";
import { CompletionStep, InitializationStep, ProbeStep } from "./EvidenceSteps";

const steps = ["开始检查", "运行环境", "学习资料", "AI 老师（可跳过）", "功能测试", "创建学习空间", "完成"];

interface WizardData {
  onboarding: OnboardingReport;
  settings: SettingsView;
  providers: ProviderCatalogEntry[];
}
function watchAuthorization<T>(promise: Promise<T>, onDenied: (cause: api.SettingsApiError) => void): Promise<T> {
  return promise.catch((cause: unknown) => {
    // Observe EACH request. Promise.all exposes only its first rejection;
    // a later 401/403 must still discard the session and secret inputs.
    if (cause instanceof api.SettingsApiError && (cause.status === 401 || cause.status === 403)) onDenied(cause);
    throw cause;
  });
}
async function readWizard(token: string, onDenied: (cause: api.SettingsApiError) => void): Promise<WizardData> {
  const [onboarding, settings, providers] = await Promise.all([
    watchAuthorization(api.getOnboarding(token), onDenied),
    watchAuthorization(api.getSettingsView(token), onDenied),
    watchAuthorization(api.getProviderCatalog(token), onDenied),
  ]);
  return { onboarding, settings, providers };
}
function safeError(cause: unknown): { code: string; message: string } {
  if (cause instanceof api.SettingsApiError) return {
    code: cause.code,
    message: `${cause.field ? `${cause.field}：` : ""}${cause.message}`,
  };
  return { code: "OPERATION_FAILED", message: "操作未完成，请重新读取已保存状态后明确重试；不会自动重放写操作。" };
}
function unfinished(status: string | undefined) { return status === "queued" || status === "running"; }

export function OnboardingWizard({ onFactsChanged }: { onFactsChanged: () => void }) {
  const session = useSettingsSession();
  const resource = useSessionResource(token => readWizard(token, session.expire));
  useEffect(() => {
    if (resource.error instanceof api.SettingsApiError && resource.error.status === 403) session.expire(resource.error);
  }, [resource.error, session]);
  if (resource.loading) return <Card><Skeleton active paragraph={{ rows: 4 }} /></Card>;
  if (resource.error || !resource.data) {
    const error = safeError(resource.error);
    return <Alert type="error" showIcon title="向导状态读取失败" description={`${error.code}：${error.message}`}
      action={<Button onClick={resource.reload}>重新读取已保存状态</Button>} />;
  }
  return <WizardContent initial={resource.data} onReload={resource.reload} onFactsChanged={onFactsChanged} />;
}

function WizardContent({ initial, onReload, onFactsChanged }: {
  initial: WizardData; onReload: () => void; onFactsChanged: () => void;
}) {
  const session = useSettingsSession();
  const token = session.token!;
  const [step, setStep] = useState(Math.min(steps.length - 1, initial.onboarding.progress.current_step));
  const [progress, setProgress] = useState<OnboardingView>(initial.onboarding.progress);
  const [draft, setDraft] = useState<DraftView | null>(initial.onboarding.draft ?? null);
  const [settings, setSettings] = useState<SettingsView>(initial.settings);
  const [report, setReport] = useState<ReadinessReport>(initial.onboarding.readiness);
  const [catalog, setCatalog] = useState<CredentialStatusView[]>(initial.settings.credential_statuses ?? []);
  const [validation, setValidation] = useState<ValidationReport | null>(null);
  const [probes, setProbes] = useState<ProbeView[]>(initial.onboarding.probes ?? []);
  const [preview, setPreview] = useState<InitializationPreview | null>(null);
  const [operation, setOperation] = useState<OperationView | null>(initial.onboarding.initialization ?? null);
  const [activation, setActivation] = useState<ActivationView | null>(null);
  const [restart, setRestart] = useState<RestartView | null>(null);
  const [diagnostics, setDiagnostics] = useState<DiagnosticsReport | null>(null);
  const [acknowledgeOverride, setAcknowledgeOverride] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<ReturnType<typeof safeError> | null>(null);
  const lock = useRef(false);
  const candidateEpoch = useRef(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  function fail(cause: unknown) {
    if (!mounted.current) return;
    if (cause instanceof api.SettingsApiError && (cause.status === 401 || cause.status === 403)) {
      session.expire(cause); // Unmount all forms and discard all secret inputs.
      return;
    }
    setError(safeError(cause));
  }
  async function run(action: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true); setError(null); setMessage(null);
    try { await action(); } catch (cause) { fail(cause); }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  async function facts() {
    const epoch = ++candidateEpoch.current;
    const [freshSettings, fresh] = await Promise.all([
      watchAuthorization(api.getSettingsView(token), session.expire),
      watchAuthorization(api.getOnboarding(token), session.expire),
    ]);
    if (!mounted.current || candidateEpoch.current !== epoch) return;
    candidateEpoch.current += 1;
    setSettings(freshSettings); setReport(fresh.readiness); setProgress(fresh.progress);
    setProbes(fresh.probes ?? []); setOperation(fresh.initialization ?? null);
    onFactsChanged();
  }
  async function remember(nextStep: number, currentDraft = draft) {
    const saved = await api.saveOnboarding(token, {
      expected_revision: progress.revision, current_step: nextStep, draft_revision: currentDraft?.revision_id ?? null,
    });
    if (!mounted.current) return;
    setProgress(saved); setStep(nextStep);
  }
  async function adopt(saved: DraftView) {
    if (!mounted.current) return;
    candidateEpoch.current += 1;
    setDraft(saved); setValidation(null); setProbes([]); setPreview(null); setOperation(null); setActivation(null);
    setMessage("设置已暂存；检查、测试、创建和启用仍需分别确认。");
    // This association is part of an explicit Save button, never a navigation
    // effect. If it fails, keep the successful draft receipt visible for retry.
    await remember(step, saved);
  }
  function save(changes: Record<string, unknown>) {
    void run(async () => {
      const saved = draft ? await api.patchDraft(token, draft.revision_id, {
        expected_revision: draft.parent_revision, changes, acknowledge_override: acknowledgeOverride,
      }) : await api.createDraft(token, {
        base_revision: settings.pending_revision ?? settings.active_revision,
        changes: { ...(settings.values ?? {}), ...changes }, acknowledge_override: acknowledgeOverride,
      });
      await adopt(saved);
    });
  }
  function credential(purpose: CredentialPurpose, secret: string, accept: (ref: CredentialReference) => void) {
    void run(async () => {
      const saved = await api.createCredential(token, { purpose, secret_value: secret });
      if (!mounted.current) return;
      const ref = referenceFromStatus(saved);
      setCatalog(previous => [...previous.filter(item => item.credential_ref !== saved.credential_ref || item.version !== saved.version), saved]);
      accept(ref);
      const name = purpose === "jwt" ? "JWT" : purpose === "provider" ? "模型" : purpose === "database" ? "数据库" : "Redis";
      setMessage(`${name} 凭据已保存；只保留引用，请显式保存或绑定到草稿。`);
    });
  }
  async function readPending() {
    const epoch = candidateEpoch.current;
    const pending = probes.filter(probe => unfinished(probe.status));
    const results = await Promise.all(pending.map(probe => watchAuthorization(api.getProbe(token, probe.probe_id), session.expire)));
    const updatedOperation = operation && unfinished(operation.status) ? await watchAuthorization(api.getOperation(token, operation.operation_id), session.expire) : operation;
    if (!mounted.current || epoch !== candidateEpoch.current) return;
    if (results.length) setProbes(previous => previous.map(item => results.find(next => next.probe_id === item.probe_id) ?? item));
    if (updatedOperation) setOperation(previous => previous?.operation_id === operation?.operation_id ? updatedOperation : previous);
  }
  // Only safe GETs may be polled. A failed read stops this cycle; the retry
  // button checks saved facts and never replays createProbe/apply/activate.
  const pollKey = probes.filter(probe => unfinished(probe.status)).map(probe => probe.probe_id).join(",") + (unfinished(operation?.status) ? operation?.operation_id : "");
  const [pollTick, setPollTick] = useState(0);
  useEffect(() => {
    if (!pollKey) return;
    const timer = window.setTimeout(() => {
      void readPending().then(() => { if (mounted.current) setPollTick(value => value + 1); }).catch(fail);
    }, 750);
    return () => window.clearTimeout(timer);
    // The key/tick controls safe reads; closures intentionally capture the
    // latest records at each scheduling point, never mutation callbacks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollKey, pollTick]);

  const valid = validation?.valid ?? draft?.status === "validated";
  const head = settings.pending_revision ?? settings.active_revision ?? null;
  const stale = Boolean(draft && draft.parent_revision !== head);
  const values = draft?.values ?? settings.values ?? null;
  const refresh = () => { void run(async () => { await readPending(); await facts(); }); };
  const verifiedCapabilities = new Set(probes.filter(item => item.status === "succeeded").map(item => `${item.profile_id}:${item.capability}`));
  const statusItems = [
    { label: draft ? "设置已暂存" : "设置尚未暂存", success: Boolean(draft) },
    { label: valid ? "设置检查通过" : "设置尚未检查通过", success: valid },
    { label: `${verifiedCapabilities.size} 项功能测试已通过`, success: probes.some(item => item.status === "succeeded") },
    { label: operation?.status === "succeeded" ? "学习空间已创建（本次操作）" : "学习空间尚未创建", success: operation?.status === "succeeded" },
    { label: settings.active_revision ? "设置已启用" : "设置尚未启用", success: Boolean(settings.active_revision) },
    { label: settings.pending_revision ? "需要重启后生效" : "无需重启", success: false },
    { label: report.business.ready ? "可以开始学习" : "还不能开始学习", success: report.business.ready },
  ];

  return <Card>
    <Space orientation="vertical" size={20} style={{ width: "100%" }}>
      <Typography.Title level={3} style={{ margin: 0 }}>首次启动向导</Typography.Title>
      <Steps current={step} responsive onChange={next => { if (!busy) setStep(next); }} items={steps.map(title => ({ title }))} />
      <Typography.Text type="secondary">步骤位置只代表流程进度，不表示任何依赖通过。切换步骤不保存；仅明确的保存/确认按钮修改服务端状态。</Typography.Text>
      <Space wrap>{statusItems.map(item => <Tag key={item.label} color={item.success ? "success" : "default"}>{item.label}</Tag>)}</Space>
      {message ? <Alert type="info" showIcon title={message} /> : null}
      {error ? <Alert type="error" showIcon title={`${error.code}：${error.message}`}
        description="如果请求中断，请先重新读取已保存状态；不会自动重试，也不会切换已启用的设置。" /> : null}
      {stale && !activation ? <Alert type="warning" showIcon title="设置已被其他窗口修改，不能直接覆盖"
        action={<Button disabled={busy || !draft} onClick={() => { void run(async () => {
          const saved = await api.createDraft(token, { base_revision: head, changes: draft!.values, acknowledge_override: acknowledgeOverride });
          await adopt(saved);
        }); }}>保存为新的设置草稿</Button>} /> : null}

      {step === 0 ? <>
        <Descriptions size="small" column={1} items={[
          { key: "environment", label: "当前环境", children: settings.environment ?? "unknown" },
          { key: "identity", label: "环境身份", children: settings.environment_id ?? "unknown" },
          { key: "config", label: "配置状态", children: settings.configuration },
          { key: "step", label: "已保存流程位置", children: progress.current_step },
          { key: "draft", label: "已保存草稿", children: draft?.revision_id ?? "尚无" },
        ]} />
        <Alert type="info" title="缺少部分配置时，设置页仍可使用"
          description="环境和启动身份由本机入口决定，本页不切换环境，也不会自动安装服务或发送付费请求。" />
      </> : null}
      {step === 1 ? <>
        <Alert type="info" title="运行环境检查针对已启用的设置"
          description="缺少配置时会如实显示；新设置的创建目标会在预览中单独核对，旧设置成功不代表新设置已通过。" />
        <Button disabled={busy} onClick={() => { void run(async () => {
          setDiagnostics(await api.runDependencyDiagnostics(token)); await facts();
        }); }}>检查运行环境</Button>
        {diagnostics ? <Space orientation="vertical">{Object.entries(diagnostics.checks).map(([name, check]) =>
          <Typography.Text key={name}>{name}：{check.status} · {check.code ?? "无错误码"} · {check.detail ?? ""}</Typography.Text>)}</Space> : null}
      </> : null}
      {step === 2 ? <BusinessStep key={draft?.revision_id ?? "empty"} values={values} catalog={catalog} busy={busy} externalOnly={Boolean(settings.warnings?.includes("EXTERNALLY_MANAGED"))} onSave={save} onCredential={credential} /> : null}
      {step === 3 ? draft ? <ModelStep key={draft.revision_id} draft={draft} catalog={catalog} providers={initial.providers} busy={busy} externalOnly={Boolean(settings.warnings?.includes("EXTERNALLY_MANAGED"))} onSave={save} onCredential={credential}
        onBind={(profileId, reference) => { void run(async () => {
          await adopt(await api.bindCredential(token, draft.revision_id, { profile_id: profileId, credential_ref: reference.credential_id, version: reference.version }));
        }); }} /> : <Alert type="info" title="请先返回“学习资料”步骤，保存基础设置。" /> : null}
      {step === 4 ? <ProbeStep key={draft?.revision_id ?? "none"} draft={draft} probes={probes} busy={busy} onRead={refresh}
        onProbe={(profileId, capability) => { if (draft) void run(async () => {
          candidateEpoch.current += 1;
          const result = await api.createProbe(token, { draft_revision: draft.revision_id, profile_id: profileId, capability, allow_billable: true, timeout_seconds: 15 });
          if (mounted.current) { candidateEpoch.current += 1; setProbes(previous => [...previous, result]); }
        }); }}
        onCancel={id => { void run(async () => {
          candidateEpoch.current += 1;
          const result = await api.cancelProbe(token, id);
          if (mounted.current) { candidateEpoch.current += 1; setProbes(previous => previous.map(probe => probe.probe_id === id ? result : probe)); }
        }); }} /> : null}
      {step === 5 ? <InitializationStep draft={draft} preview={preview} operation={operation} busy={busy} onRead={refresh}
        onPreview={() => { if (draft) void run(async () => { setPreview(await api.previewInitialization(token, { draft_revision: draft.revision_id })); }); }}
        onApply={() => { if (preview && draft) void run(async () => {
          const confirmation = preview.preview_id;
          candidateEpoch.current += 1;
          setPreview(null); // A lost response is not permission to replay.
          const result = await api.applyInitialization(token, { confirmation_id: confirmation, expected_revision: draft.revision_id });
          if (mounted.current) { candidateEpoch.current += 1; setOperation(result); }
          onFactsChanged();
        }); }} /> : null}
      {step === 6 ? <CompletionStep draft={draft} valid={valid} settings={settings} report={report} activation={activation} restart={restart} busy={busy}
        onActivate={() => { if (draft) void run(async () => {
          const result = await api.activateDraft(token, draft.revision_id, { expected_active_revision: settings.active_revision, probe_ids: probes.filter(item => item.status === "succeeded").map(item => item.probe_id), acknowledge_override: acknowledgeOverride });
          if (mounted.current) setActivation(result);
          await facts();
        }); }}
        onRestart={() => { if (head) void run(async () => { setRestart(await api.restartApplication(token, { expected_revision: head })); }); }}
        onFinish={() => { void run(async () => { await remember(6); setMessage("向导完成步骤已保存；是否可以开始学习及 AI 状态仍以实时状态为准。"); }); }} /> : null}

      {draft ? <Space orientation="vertical" style={{ width: "100%" }}>
        <Button disabled={busy || stale} onClick={() => { void run(async () => {
          const result = await api.validateDraft(token, draft.revision_id, { expected_revision: draft.parent_revision });
          if (mounted.current) setValidation(result);
        }); }}>检查设置</Button>
        {validation && !validation.valid ? validation.errors?.map(issue => <Alert key={`${issue.field}:${issue.code}`} type="error" title={`${issue.field} · ${issue.code}：${issue.message}`} />) : null}
      </Space> : null}
      <Checkbox checked={acknowledgeOverride} disabled={busy} onChange={event => setAcknowledgeOverride(event.target.checked)}>
        如果提示环境覆盖，我确认只修改保存值；实际是否生效仍以运行环境为准
      </Checkbox>
      {settings.sources && Object.keys(settings.sources).length > 0 ? <Typography.Text type="secondary">
        当前设置来源：{Object.entries(settings.sources).map(([field, source]) => `${field}=${source.source}${source.effective ? "" : "（保存值未生效）"}`).join("；")}
      </Typography.Text> : null}
      {settings.warnings?.map(warning => <Alert key={warning} type="warning" title={warning} />)}
      <Space wrap>
        <Button disabled={busy || step === 0} onClick={() => setStep(current => current - 1)}>上一步</Button>
        <Button disabled={busy || step === steps.length - 1} onClick={() => setStep(current => current + 1)}>下一步</Button>
        <Button disabled={busy || step === steps.length - 1} onClick={() => { void run(async () => { await remember(step + 1); }); }}>保存并继续</Button>
        <Button disabled={busy} onClick={onReload}>重新读取（丢弃未保存输入）</Button>
      </Space>
      <Typography.Text type="secondary">重新授权后只恢复已保存的非敏感事实。离开本步骤会清空秘密输入；刷新不会重放 Probe、迁移、激活或重启。</Typography.Text>
    </Space>
  </Card>;
}
