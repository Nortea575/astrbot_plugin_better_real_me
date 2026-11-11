const $ = (selector, root = document) => root.querySelector(selector);
const SECTION_KEYS = ['states','schedule','location'];
const SECTION_LABELS = {states:'状态',schedule:'日程',location:'位置'};

export function mountSharing({api,getData,toast}) {
  const form=$('#sharingForm'),bindingField=$('#sharingBindingField'),bindingInput=$('#sharingBindingUrl');
  const accountSelect=$('#sharingAccount'),submitButton=$('#sharingSubmit'),manage=$('#sharingManage');
  const accountField=$('#sharingAccountField'),accountDisplay=$('#sharingAccountDisplay');
  const accountAvatar=$('#sharingAccountAvatar'),accountDisplayName=$('#sharingDetectedAccountName'),accountDisplayHint=$('#sharingDetectedAccountHint');
  const errorNode=$('#sharingError'),badge=$('#sharingBadge'),toggle=$('#sharingToggle');
  let status=null,accounts=[],busy=false,draftDirty=false,loaded=false;

  const sections=()=>Object.fromEntries(SECTION_KEYS.map(key=>[key,$(`[name="section"][value="${key}"]`,form).checked]));
  const selectedAccount=()=>String(accountSelect.value || '');
  const messageOf=error=>String(error?.message || error?.error || '操作失败，请检查连接后重试。').slice(0,500);
  function setError(message='') {
    errorNode.textContent=message;
    errorNode.hidden=!message;
  }
  function setBusy(value) {
    busy=value;
    render();
  }
  function accountId(account) {return String(account?.account_id || account?.id || '');}
  function accountName(id) {
    const account=accounts.find(item=>accountId(item)===String(id));
    return account?.name || '已识别的 QQ 账号';
  }
  function collectAccounts(topAccounts=[],lifeAccounts=[]) {
    const merged=new Map();
    for (const item of [...(Array.isArray(topAccounts)?topAccounts:[]),...(Array.isArray(lifeAccounts)?lifeAccounts:[])]) {
      const id=accountId(item);
      if (id) merged.set(id,{...(merged.get(id)||{}),...item,id});
    }
    accounts=[...merged.values()];
    const current=selectedAccount();
    accountSelect.replaceChildren();
    if (!accounts.length && !status?.connected) {
      const option=document.createElement('option');option.value='';option.textContent='暂无已识别的 QQ 账号';accountSelect.append(option);
      return;
    }
    for (const account of accounts) {
      const option=document.createElement('option');option.value=accountId(account);
      option.textContent=`${account.name || '未命名账号'} · 已识别`;
      accountSelect.append(option);
    }
    const preferred=status?.connected?String(status.account_id || ''):current;
    if (preferred && ![...accountSelect.options].some(option=>option.value===preferred)) {
      const option=document.createElement('option');option.value=preferred;option.textContent='已连接的 QQ 账号';accountSelect.append(option);
    }
    if (preferred) accountSelect.value=preferred;
    if (!accountSelect.value && accountSelect.options.length) accountSelect.selectedIndex=0;
  }
  function renderAccountChoice() {
    const connected=Boolean(status?.connected),selected=accounts.find(item=>accountId(item)===selectedAccount());
    accountField.hidden=connected || accounts.length===1;
    accountDisplay.hidden=connected || accounts.length!==1;
    if (connected) return;
    if (accounts.length===1) {
      const account=accounts[0],name=String(account.name || 'QQ 账号');
      accountDisplayName.textContent=name;
      accountDisplayHint.textContent='已自动识别';
      accountAvatar.replaceChildren();
      accountAvatar.textContent=[...name.trim()][0] || 'Q';
      const url=String(account.avatar_url || '');
      if (url) {
        const image=document.createElement('img');image.alt='';image.referrerPolicy='no-referrer';
        image.addEventListener('error',()=>{image.remove();accountAvatar.textContent=[...name.trim()][0] || 'Q';},{once:true});
        image.src=url;accountAvatar.textContent='';accountAvatar.append(image);
      }
    } else if (accounts.length>1) {
      const account=selected;
      if (account) {
        accountDisplayName.textContent=String(account.name || 'QQ 账号');
        accountDisplayHint.textContent='当前选择';
      }
    }
  }
  function statusLine() {
    if (!status?.connected) return '尚未连接。绑定链接只在提交时使用一次，输入后不会保存在浏览器中。';
    if (status.pending_disable) return '本地已停止发送数据，正在等待分享服务确认关闭。';
    if (status.last_success) return `最近同步 ${formatDate(status.last_success)}`;
    return '连接已建立，等待首次同步。';
  }
  function formatDate(value) {
    const date=new Date(value);
    return Number.isNaN(date.getTime())?String(value):new Intl.DateTimeFormat('zh-CN',{dateStyle:'medium',timeStyle:'short'}).format(date);
  }
  function render() {
    const snapshot=getData() || {};
    collectAccounts(snapshot.accounts,snapshot.life?.accounts);
    const connected=Boolean(status?.connected);
    renderAccountChoice();
    bindingField.hidden=connected;
    bindingInput.required=!connected;
    bindingInput.disabled=busy || connected;
    accountSelect.disabled=busy || connected;
    for (const input of form.querySelectorAll('[name="section"]')) input.disabled=busy;
    submitButton.disabled=busy || (!connected && (!accounts.length || !selectedAccount()));
    submitButton.textContent=connected?'保存分享范围':'绑定并开启分享';
    $('#sharingConnectionHint').textContent=connected
      ? '选择公开内容并保存；QQ 账号在断开前保持固定。'
      : accounts.length>1?'粘贴一次性绑定链接，并选择要连接的 QQ 账号。':accounts.length?'粘贴一次性绑定链接即可连接已识别的 QQ 账号。':'QQ 账号连接后会自动出现在此处。';
    manage.hidden=!connected;
    badge.dataset.state=!connected?'off':status.pending_disable?'pending':status.enabled&&status.remote_public_enabled?'on':status.enabled?'waiting':'paused';
    badge.textContent=!connected?'未连接':status.pending_disable?'等待关闭':status.enabled&&status.remote_public_enabled?'分享中':status.enabled?'等待远端开启':'已暂停';
    const accountText=connected?accountName(status.account_id):'';
    $('#sharingAccountName').textContent=connected?`连接账号：${accountText}`:'';
    toggle.setAttribute('aria-checked',String(Boolean(status?.enabled)));
    toggle.disabled=busy || !connected || Boolean(status?.pending_disable);
    toggle.title=status?.enabled?'关闭公开展示':'开启公开展示';
    $('#sharingToggleLabel').textContent=status?.enabled?'公开展示已开启':'公开展示已关闭';
    $('#sharingSyncLine').textContent=statusLine();
    $('#sharingPendingDisable').hidden=!status?.pending_disable;
    const lastError=String(status?.last_error || '');
    $('#sharingLastError').textContent=lastError?`最近一次错误：${lastError}`:'';
    $('#sharingLastError').hidden=!lastError;
    const link=String(status?.view_url || '');
    $('#sharingPublicLink').hidden=!connected || !link;
    $('#sharingViewUrl').value=link;
    if (!draftDirty) {
      for (const key of SECTION_KEYS) $(`[name="section"][value="${key}"]`,form).checked=connected?Boolean(status?.sections?.[key]):true;
    }
  }
  async function invoke(endpoint,body,context) {
    const result=await api(endpoint,body,context);
    if (result?.error) throw Error(String(result.error));
    if (result?.status==='error') throw Error(String(result.message || '操作失败，请重试。'));
    return result;
  }
  function acceptStatus(incoming,topAccounts,lifeAccounts) {
    if (Array.isArray(topAccounts) || Array.isArray(lifeAccounts)) collectAccounts(topAccounts,lifeAccounts);
    if (incoming && typeof incoming==='object' && !Array.isArray(incoming)) {
      status={...(status || {}),...incoming};loaded=true;
      if (!draftDirty && incoming.sections) {
        for (const key of SECTION_KEYS) $(`[name="section"][value="${key}"]`,form).checked=Boolean(incoming.sections[key]);
      }
      render();
    } else if (Array.isArray(lifeAccounts)) render();
  }
  async function load() {
    setError('');
    try {
      const incoming=await invoke('sharing/status');
      acceptStatus(incoming,getData()?.accounts || [],getData()?.life?.accounts || []);
      loaded=true;
    } catch (error) {
      setError(`无法读取分享状态：${messageOf(error)}`);
    }
  }
  function currentAccountId() {return String(status?.account_id || selectedAccount());}
  async function saveSettings(enabled) {
    const account_id=currentAccountId();
    if (!account_id) throw Error('请选择一个已识别的真实 QQ 账号。');
    const result=await invoke('sharing/settings',{enabled:Boolean(enabled),account_id,sections:sections()});
    acceptStatus(result,getData()?.accounts || [],getData()?.life?.accounts || []);
    draftDirty=false;
    render();
  }

  form.addEventListener('change',event=>{
    if (event.target.matches('[name="section"],#sharingAccount')) draftDirty=true;
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if (busy) return;
    setError('');
    const connecting=!status?.connected;
    const pickedSections=sections();
    const account_id=connecting && accounts.length>1?selectedAccount():connecting?'':currentAccountId();
    if (connecting && accounts.length>1 && !account_id) {setError('请选择一个已识别的真实 QQ 账号。');return;}
    const binding_url=bindingInput.value.trim();
    if (connecting && !binding_url) {setError('请粘贴一次性绑定 URL。');return;}
    setBusy(true);
    try {
      const result=connecting
        ? await invoke('sharing/connect',{binding_url,...(account_id?{account_id}:{}),sections:pickedSections},null)
        : await invoke('sharing/settings',{enabled:Boolean(status?.enabled),account_id,sections:pickedSections});
      acceptStatus(result,getData()?.accounts || [],getData()?.life?.accounts || []);
      draftDirty=false;
      render();
      toast?.(connecting?'已绑定并开启分享':'分享范围已保存');
    } catch (error) {
      setError(messageOf(error));
    } finally {
      if (connecting) bindingInput.value='';
      setBusy(false);
    }
  });
  toggle.addEventListener('click',async()=>{
    if (busy || !status?.connected || status.pending_disable) return;
    setError('');setBusy(true);
    try {
      await saveSettings(!status.enabled);
      toast?.(status?.enabled?'公开展示已开启':'公开展示已关闭');
    } catch (error) {setError(messageOf(error));}
    finally {setBusy(false);}
  });
  $('#sharingDisconnect').addEventListener('click',async()=>{
    if (busy || !status?.connected) return;
    const name=accountName(status.account_id);
    if (!window.confirm(`断开“${name}”的分享连接？服务端公开页将关闭。`)) return;
    setError('');setBusy(true);
    try {
      const result=await invoke('sharing/disconnect',{},null);
      acceptStatus(result,getData()?.accounts || [],getData()?.life?.accounts || []);
      draftDirty=false;
      toast?.('分享连接已断开');
    } catch (error) {setError(messageOf(error));}
    finally {setBusy(false);}
  });
  $('#sharingCopyViewUrl').addEventListener('click',async()=>{
    const value=$('#sharingViewUrl').value;
    if (!value) return;
    let copied=false;
    try {
      await navigator.clipboard.writeText(value);
      copied=true;
    } catch {
      const input=$('#sharingViewUrl');
      input.focus();input.select();
      try {copied=document.execCommand('copy');} catch {}
    }
    toast?.(copied?'公开链接已复制':'链接已选中，请按 Ctrl+C 复制');
  });

  for (const key of SECTION_KEYS) $(`[name="section"][value="${key}"]`,form).setAttribute('aria-label',`公开${SECTION_LABELS[key]}`);
  render();
  return {load,acceptStatus,get loaded(){return loaded;}};
}
