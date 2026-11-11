const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const copy = value => structuredClone(value ?? {});
const DEFAULT_PRESET_ID = 'neighborhood_life';
const conditionOps = [['gte','不低于'],['gt','高于'],['lte','不高于'],['lt','低于'],['eq','等于'],['ne','不等于']];
const effectOps = [['add','增加 / 减少'],['set','设为']];
const ACTION_CATEGORIES = ['生活起居','工作安排','家务维护','休闲放松','外出安排','购物补给','街区活动'];
const memoryLabels = {disabled:'未同步',pending:'等待同步',processing:'正在同步',saved:'已同步',failed:'同步失败',ignored:'本次未写入'};

export function mountLife({api,run,toast,getData,escape,onChange}) {
  const panel=$('#lifePanel');
  let overview={presets:[],accounts:[],actions:[],events:[],options:{personas:[],providers:[]},integrations:{livingmemory:{installed:false,ready:false,version:''}}};
  let actionMarkupKey='';
  let actionCategoryFilter='*';
  let pendingAccounts=new Set();
  let settingsDrafts=new Map();

  const esc=value=>escape(String(value ?? ''));
  const accountId=account=>String(account?.id || `${account?.platform_id || 'qq'}:${account?.qq || ''}`);
  const presetById=id=>overview.presets.find(item=>String(item.id)===String(id)) || null;
  const previewPreset=()=>presetById(DEFAULT_PRESET_ID);
  const enabled=account=>typeof account.enabled==='boolean' ? account.enabled : (presetById(DEFAULT_PRESET_ID)?.enabled_accounts || []).includes(accountId(account));
  const presetConfigured=account=>typeof account.preset_configured==='boolean'?account.preset_configured:Boolean(String(account.preset_id || '').trim());
  const selectedPersona=account=>settingsDrafts.get(accountId(account))?.persona_id ?? String(account.persona_id || '');
  const selectedSettings=account=>settingsDrafts.get(accountId(account))?.settings || {
    memory_enabled:Boolean(account.memory_enabled),
    model_enabled:Boolean(account.model_enabled),
    provider_id:String(account.provider_id || '')
  };
  const dirtySettings=account=>Boolean(settingsDrafts.get(accountId(account))?.dirty);
  const currentRooms=()=>getData().indoor?.rooms || [];
  function roomDisplayName(room) {
    const indoor=getData().indoor || {},groups=new Map((indoor.groups || []).map(group=>[String(group.id),group])),path=[],seen=new Set();let group=groups.get(String(room.group_id || ''));
    while(group && !seen.has(String(group.id))){seen.add(String(group.id));path.unshift(group.name || '');group=group.parent_id?groups.get(String(group.parent_id)):null;}
    return [...path.filter(Boolean),room.name || room.id].join(' / ');
  }
  const stateCatalog=()=>{
    const definitions=getData().definitions || [];
    const states=new Map();
    for(const item of overview.presets.flatMap(preset=>preset.contents?.states || []))if(item?.id!=null)states.set(String(item.id),{...states.get(String(item.id)),...item});
    return [...states.values()].map(item=>({...item,...(definitions.find(definition=>definition.id===item.id) || {})}));
  };
  const scheduleItems=contents=>Array.isArray(contents.schedule)?contents.schedule:Object.entries(contents.schedule || {}).filter(([key])=>key!=='timezone').map(([id,item])=>({id,...item,name:item?.name || id}));
  function contentCount(contents) {
    const schedule=scheduleItems(contents);
    const roomGroups=roomGroupsFor(contents),rootName=roomGroups.find(group=>!group.parent_id)?.name;
    return '状态 '+(contents.states?.length || 0)+' · 指标 '+(contents.groups?.length || 0)+' · 生活窗口 '+schedule.length+' · 房间组 '+roomGroups.length+(rootName?'（含'+rootName+'）':'')+' · 房间 '+(contents.rooms?.length || 0)+' · 动作 '+(contents.actions?.length || 0);
  }
  const stateById=id=>stateCatalog().find(item=>item.id===id) || null;
  function categoryLabels(contents=previewPreset()?.contents || {}) {
    const categories=[...(getData().categories || []),...(contents.categories || [])],map=new Map();
    for(const item of categories)if(item?.id!=null && item?.name)map.set(String(item.id),String(item.name));
    return map;
  }
  function contentCategory(item,contents,field='category') {
    const value=String(item?.[field==='state-category'?'category':field] || '').trim();if(!value)return '未分类';
    const label=categoryLabels(contents).get(value);return label || (field==='state-category'?'未分类':value);
  }
  function groupedContents(items,contents,kind,field='category') {
    const groups=new Map();for(const item of items){const label=contentCategory(item,contents,field);if(!groups.has(label))groups.set(label,[]);groups.get(label).push(item);}
    return [...groups.entries()].sort(([a],[b])=>a.localeCompare(b,'zh-CN')).map(([label,rows])=>`<section class="life-content-category"><h5>${esc(label)} <span>${rows.length}</span></h5><ul>${rows.map(row=>briefItem(row,kind)).join('')}</ul></section>`).join('');
  }
  function actionCategoryOptions(selected='') {
    const existing=[...overview.actions.map(action=>String(action.category || '').trim()),...overview.presets.flatMap(item=>item.contents?.actions || []).map(action=>String(action.category || '').trim())].filter(Boolean);
    const categories=[...new Set([...ACTION_CATEGORIES,...existing,selected].filter(Boolean))];
    return categories.map(category=>`<option value="${esc(category)}" ${category===selected?'selected':''}>${esc(category)}</option>`).join('');
  }

  function unwrapLife(result) {
    if (result?.life && Array.isArray(result.life.accounts)) return result.life;
    if (result?.data?.life && Array.isArray(result.data.life.accounts)) return result.data.life;
    if (Array.isArray(result?.accounts) && (Array.isArray(result.presets) || Array.isArray(result.actions))) return result;
    return null;
  }
  function accept(result) {
    const incoming=unwrapLife(result) || result || {};
    overview={...overview,...incoming,
      presets:[...(incoming.presets || overview.presets || [])],
      accounts:[...(incoming.accounts || overview.accounts || [])],
      actions:[...(incoming.actions || overview.actions || [])],
      events:[...(incoming.events || overview.events || [])],
      options:{personas:[],providers:[],...(overview.options || {}),...(incoming.options || {})},
      integrations:{...(overview.integrations || {}),...(incoming.integrations || {})}
    };
    overview.options.personas=[...(overview.options.personas || [])];
    overview.options.providers=[...(overview.options.providers || [])];
    actionMarkupKey='';
    render();
  }
  function applyResult(result) {
    const next=unwrapLife(result);
    if (next) {
      accept(next);
      onChange?.(overview);
      return overview;
    }
    return overview;
  }
  function showPanelError(message='') {
    const node=$('#lifeLoadError');
    node.textContent=message;
    node.hidden=!message;
  }
  function formatTime(value) {
    if (!value) return '—';
    const date=new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
  }
  function itemName(item,fallback='未命名') {return String(item?.name || item?.title || fallback);}
  function roomGroupsFor(contents) {
    const rooms=Array.isArray(contents.rooms)?contents.rooms:[];
    const provided=Array.isArray(contents.room_groups)?contents.room_groups:[];
    if(provided.length)return provided;
    const indoor=getData().indoor || {},all=indoor.groups || [],byId=new Map(all.map(group=>[String(group.id),group])),used=new Set();
    for(const room of rooms){
      let group=byId.get(String(room.group_id || '')),seen=new Set();
      while(group && !seen.has(String(group.id))){seen.add(String(group.id));used.add(String(group.id));group=byId.get(String(group.parent_id || ''));}
    }
    return all.filter(group=>used.has(String(group.id)));
  }
  function groupDepth(group,byId) {
    let depth=0,parent=byId.get(String(group.parent_id || '')),seen=new Set([String(group.id)]);
    while(parent && !seen.has(String(parent.id))){seen.add(String(parent.id));depth++;parent=byId.get(String(parent.parent_id || ''));}
    return depth;
  }
  function roomNameInContents(room,contents) {
    const all=[...(contents.room_groups || []),...(getData().indoor?.groups || [])],byId=new Map();
    for(const group of all)if(group?.id!=null)byId.set(String(group.id),group);
    const path=[],seen=new Set();let group=byId.get(String(room.group_id || ''));
    while(group && !seen.has(String(group.id))){seen.add(String(group.id));path.unshift(String(group.name || ''));group=byId.get(String(group.parent_id || ''));}
    return [...path.filter(Boolean),String(room.name || room.id || '未知房间')].join(' / ');
  }
  function roomPreviewMarkup(contents) {
    const rooms=Array.isArray(contents.rooms)?contents.rooms:[],groups=roomGroupsFor(contents),byId=new Map(groups.map(group=>[String(group.id),group]));
    const rows=groups.map((group,index)=>({group,index,depth:groupDepth(group,byId)})).sort((a,b)=>a.depth-b.depth || a.index-b.index);
    const list=rows.map(({group,depth})=>{
      const count=rooms.filter(room=>String(room.group_id || '')===String(group.id)).length;
      return '<li class="life-room-group" style="--life-group-depth:'+Math.min(depth,4)+'"><strong>'+esc(group.name || '未命名区域')+'</strong><span>'+count+' 个房间</span>'+(group.description?'<small>'+esc(group.description)+'</small>':'')+'</li>';
    }).join('');
    if(list)return '<ul class="life-room-groups">'+list+'</ul>';
    if(!rooms.length)return '<p class="help">街区生活还没有提供房间。</p>';
    const grouped=new Map();
    for(const room of rooms){const name=room.group_id?roomNameInContents(room,contents).split(' / ').slice(0,-1).join(' / ') || '未分组':'未分组';grouped.set(name,(grouped.get(name)||0)+1);}
    return '<ul class="life-room-groups">'+[...grouped].map(([name,count])=>'<li class="life-room-group"><strong>'+esc(name)+'</strong><span>'+count+' 个房间</span></li>').join('')+'</ul>';
  }
  function connectionSummary(contents) {
    const rooms=Array.isArray(contents.rooms)?contents.rooms:[],byId=new Map(rooms.map(room=>[String(room.id),room]));
    const connections=Array.isArray(contents.connections)?contents.connections:getData().indoor?.connections || [];
    const valid=connections.filter(edge=>byId.has(String(edge.a)) && byId.has(String(edge.b))).sort((first,second)=>{
      const within=edge=>String(byId.get(String(edge.a))?.group_id || '')===String(byId.get(String(edge.b))?.group_id || '');
      return Number(within(first))-Number(within(second));
    });
    if(!valid.length)return '';
    const shown=valid.slice(0,4).map(edge=>roomNameInContents(byId.get(String(edge.a)),contents)+' ↔ '+roomNameInContents(byId.get(String(edge.b)),contents));
    return '已配置的房间连接：'+shown.join('；')+(valid.length>shown.length?'；另有 '+(valid.length-shown.length)+' 处连接':'');
  }
  function briefItem(item,kind) {
    const name=esc(itemName(item,kind));
    if (kind==='日程') {
      const start=item.start || item.start_time || '';
      const end=item.end || item.end_time || '';
      const time=start || end ? `${start || '—'}–${end || '—'}` : '';
      const days=Array.isArray(item.days)?item.days.map(Number):[];
      const dayText=days.length===7?'每天':JSON.stringify(days)===JSON.stringify([0,1,2,3,4])?'工作日':JSON.stringify(days)===JSON.stringify([5,6])?'周末':days.length?days.map(day=>['周一','周二','周三','周四','周五','周六','周日'][day]).join('、'):'不限日期';
      return `<li><strong>${name}</strong>${time?`<span>${esc(time)} · ${esc(dayText)}</span>`:''}<small>${item.enabled===false?'已暂停':'可执行时段'}${item.description?` · ${esc(item.description)}`:''}</small></li>`;
    }
    if (kind==='房间') return `<li><strong>${esc(item.group_id?roomDisplayName(item):itemName(item,kind))}</strong>${item.description?`<small>${esc(item.description)}</small>`:''}</li>`;
    if (kind==='动作') {
      const steps=(item.steps || []).map(step=>step.name || step.room_name || '一个生活阶段').filter(Boolean);
      return '<li><strong>'+name+'</strong>'+(steps.length?'<small>'+esc(steps.join(' → '))+'</small>':'')+(item.description?'<small>'+esc(item.description)+'</small>':'');
    }
    return `<li><strong>${name}</strong>${item.description?`<small>${esc(item.description)}</small>`:''}</li>`;
  }
  function renderContents() {
    const host=$('#lifePresetContents'),item=previewPreset();
    if (!item) {
      showPanelError('生活预设暂时不可用；重试同步后会重新读取。');
      host.innerHTML='<p class="help">预设内容尚未加载。</p>';
      $('#lifePresetStatus').textContent='暂不可用';
      $('#lifeTitle').textContent='街区生活预览';
      $('#lifePresetDescription').textContent='';
      $('#lifePresetInstallNote').textContent='';
      $('#lifePresetPlaces').innerHTML='<p class="help">暂无生活空间。</p>';
      $('#lifePresetConnections').hidden=true;
      return;
    }
    showPanelError('');
    const contents=item.contents || {};
    const schedule=scheduleItems(contents);
    const states=contents.states || [],groups=contents.groups || [],rooms=contents.rooms || [],actions=contents.actions || [];
    const sections=[
      ['状态',states,groupedContents(states,contents,'状态','state-category')],
      ['综合指标',groups,groupedContents(groups,contents,'综合指标','state-category')],
      ['生活窗口',schedule,groupedContents(schedule,contents,'日程')],
      ['房间',rooms,roomPreviewMarkup(contents)],
      ['动作',actions,groupedContents(actions,contents,'动作')]
    ];
    host.innerHTML=sections.map(([kind,items,body])=>`<section class="life-content-section"><div class="life-content-heading"><h4>${kind}</h4><span>${items.length}</span></div>${items.length?body:`<p class="help">预设没有提供${kind}。</p>`}</section>`).join('');
    $('#lifeTitle').textContent=item.name || '街区生活';
    $('#lifePresetDescription').textContent=item.description || '';
    $('#lifeTemplateTitle').textContent=(item.name || '街区生活')+' 内容';
    $('#lifePresetInstallNote').textContent=item.installed?'街区空间已添加；暂停后重新启用会沿用已有房间与记录。':'首次为某个 QQ 启用时，会添加街区空间与动作；已有内容会保留。';
    $('#lifePresetPlaces').innerHTML=roomPreviewMarkup(contents);
    const connections=connectionSummary(contents),connectionNode=$('#lifePresetConnections');
    connectionNode.textContent=connections;
    connectionNode.hidden=!connections;
    $('#lifeContentCount').textContent=contentCount(contents);
    const activeCount=overview.accounts.filter(account=>enabled(account) && String(item.id)===DEFAULT_PRESET_ID).length;
    $('#lifePresetStatus').textContent=activeCount?`已为 ${activeCount} 个 QQ 启用`:(item.installed?'已添加 · 默认关闭':'首次启用时添加');
    $('#lifePresetStatus').classList.toggle('is-active',activeCount>0);
  }
  function personaOptions(account) {
    const selected=selectedPersona(account);
    const options=overview.options.personas.map((persona,index)=>{
      const id=String(persona.id || persona.persona_id || '');
      const name=String(persona.name || persona.display_name || `已有 persona ${index+1}`);
      return `<option value="${esc(id)}" ${selected===id?'selected':''}>${esc(name)}</option>`;
    });
    if (selected && !overview.options.personas.some(persona=>String(persona.id || persona.persona_id || '')===selected)) options.unshift(`<option value="${esc(selected)}" selected>当前使用的 persona</option>`);
    return `<option value="" ${selected?'':'selected'}>沿用此 QQ 现有人格</option>${options.join('')}`;
  }
  function providerOptions(account) {
    const value=selectedSettings(account).provider_id;
    const options=overview.options.providers.map((provider,index)=>{
      const id=String(provider.id || provider.provider_id || '');
      const name=String(provider.name || provider.display_name || `已配置服务商 ${index+1}`);
      return `<option value="${esc(id)}" ${value===id?'selected':''}>${esc(name)}</option>`;
    });
    if (value && !overview.options.providers.some(provider=>String(provider.id || provider.provider_id || '')===value)) options.unshift(`<option value="${esc(value)}" selected>当前已选的模型服务商</option>`);
    return `<option value="" ${value?'':'selected'}>不指定</option>${options.join('')}`;
  }
  function createAccountCard(account) {
    const id=accountId(account),name=String(account.name || `QQ ${account.qq || ''}`),qq=String(account.qq || '');
    const initials=Array.from(name.trim())[0] || Array.from(qq)[0] || '?';
    const avatar=String(account.avatar_url || '');
    const template=document.createElement('template');
    template.innerHTML=`<article class="life-account" data-account-id="${esc(id)}">
      <header class="life-account-heading"><span class="life-avatar">${avatar?`<img src="${esc(avatar)}" alt="" loading="lazy"><span hidden>${esc(initials)}</span>`:`<span>${esc(initials)}</span>`}</span><span class="life-account-identity"><strong>${esc(name)}</strong><small>QQ ${esc(qq || '—')}</small></span><span class="life-account-badge" data-life-badge></span></header>
      <div class="life-account-live" data-life-live></div>
      <div class="life-account-toggle"><button class="primary" type="button" data-life-toggle></button></div>
      <div class="life-account-memory" data-life-memory></div>
      <form class="life-account-settings" data-life-settings novalidate>
        <label>生活人格（可选）<select data-settings-field="persona_id">${personaOptions(account)}</select><small>只指定这个 QQ 使用的已有 persona；不会新建角色或替换聊天身份。</small></label>
        <div class="life-settings-toggles"><label><input type="checkbox" data-settings-field="memory_enabled"><span>同步生活记忆</span></label><label><input type="checkbox" data-settings-field="model_enabled"><span>由模型辅助选择下一步</span></label></div>
        <p class="life-memory-note" data-life-memory-note></p>
        <label>模型服务商<select data-settings-field="provider_id">${providerOptions(account)}</select></label>
        <p class="life-provider-note" data-life-provider-note></p>
        <div class="life-settings-error" data-life-settings-error hidden></div>
        <div class="life-account-actions"><button class="secondary small" type="submit" data-life-save-settings>保存账号设置</button></div>
      </form>
      <section class="life-events" data-life-events aria-label="近期生活事件"></section>
    </article>`;
    const node=template.content.firstElementChild;
    const memory=$('[data-settings-field="memory_enabled"]',node),model=$('[data-settings-field="model_enabled"]',node);
    memory.checked=Boolean(account.memory_enabled);
    model.checked=Boolean(account.model_enabled);
    return node;
  }
  function ensureOptions(select,markup,value) {
    if (select!==document.activeElement && select.dataset.optionMarkup!==markup) {
      select.innerHTML=markup;select.dataset.optionMarkup=markup;
    }
    if (select!==document.activeElement && [...select.options].some(option=>option.value===value)) select.value=value;
  }
  function accountMemory(account,node) {
    const memory=account.memory || {},label=memoryLabels[memory.status] || memory.status || '未同步';
    const pending=Number(memory.pending || 0);
    const error=memory.last_error ? ` · ${memory.last_error}` : '';
    const integration=overview.integrations?.livingmemory || {},ready=Boolean(integration.installed && integration.ready);
    $('[data-life-memory]',node).textContent=`记忆同步 · ${ready?label:'暂不可用'}${pending?` · ${pending} 条待处理`:''}${error}`;
    $('[data-life-memory]',node).classList.toggle('is-error',memory.status==='failed' || !ready);
  }
  function renderLiveAccount(account,node) {
    const on=enabled(account),activity=on?account.current_activity:null;
    const badge=$('[data-life-badge]',node);
    badge.textContent=on?'自动生活已启用':presetConfigured(account)?'自动生活已暂停':'未启用 · 默认关闭';
    badge.classList.toggle('is-active',on);
    const host=$('[data-life-live]',node);
    if (!host.dataset.ready) {
      host.innerHTML='<dl><div><dt>当前活动</dt><dd data-live-activity></dd></div><div><dt>当前阶段</dt><dd data-live-step></dd></div><div><dt>剩余时间</dt><dd data-live-remaining></dd></div><div><dt>所在房间</dt><dd data-live-room></dd></div><div><dt>活动房间</dt><dd data-live-activity-room></dd></div><div><dt>下一次安排</dt><dd data-live-next></dd></div></dl>';
      host.dataset.ready='true';
    }
    const set=(selector,value)=>$(selector,host).textContent=value;
    set('[data-live-activity]',activity?.name || (on?'暂无进行中的活动':presetConfigured(account)?'自动行为已暂停':'尚未启用自动生活'));
    set('[data-live-step]',activity?(activity.step_name || `第 ${Number(activity.step_index || 0)+1} 阶段${activity.step_count?` / ${activity.step_count}`:''}`):'—');
    let remaining='—';
    if (activity?.ends_at) {
      const ms=new Date(activity.ends_at).getTime()-Date.now();
      remaining=ms<=0?'即将结束':ms<60000?'不到 1 分钟':`${Math.ceil(ms/60000)} 分钟`;
    }
    set('[data-live-remaining]',remaining);
    const occupant=(getData().indoor_runtime?.occupants || []).find(person=>person.life && person.account_id===accountId(account));
    const actualRoomId=occupant?.room_id || account.current_room_id || '';
    const actualRoom=currentRooms().find(item=>item.id===actualRoomId);const actualRoomName=actualRoom?roomDisplayName(actualRoom):account.current_room_name || '';
    const activityRoomData=currentRooms().find(item=>item.id===activity?.room_id),activityRoom=activityRoomData?roomDisplayName(activityRoomData):activity?.room_name || '';
    set('[data-live-room]',actualRoomName || (on?'等待分配房间':'—'));
    set('[data-live-activity-room]',activityRoom || '—');
    set('[data-live-next]',on?formatTime(account.next_due_at):'—');
    accountMemory(account,node);
    const id=accountId(account),pending=pendingAccounts.has(id);
    const toggle=$('[data-life-toggle]',node);
    toggle.textContent=on?'暂停自动行为':'启用街区生活';
    toggle.setAttribute('aria-pressed',String(on));
    toggle.disabled=pending;
    const settings=$('[data-life-settings]',node);
    const draft=settingsDrafts.get(accountId(account));
    if (!draft?.dirty) {
      const persona=$('[data-settings-field="persona_id"]',settings);
      const nextPersona=String(account.persona_id || '');
      const personaMarkup=personaOptions(account);
      ensureOptions(persona,personaMarkup,nextPersona);
      const memoryInput=$('[data-settings-field="memory_enabled"]',settings),modelInput=$('[data-settings-field="model_enabled"]',settings),provider=$('[data-settings-field="provider_id"]',settings);
      memoryInput.checked=Boolean(account.memory_enabled);modelInput.checked=Boolean(account.model_enabled);
      const providerMarkup=providerOptions(account),providerValue=String(account.provider_id || '');
      ensureOptions(provider,providerMarkup,providerValue);
    }
    const providers=overview.options.providers;
    const livingMemory=overview.integrations?.livingmemory || {},memoryReady=Boolean(livingMemory.installed && livingMemory.ready);
    const memoryInput=$('[data-settings-field="memory_enabled"]',settings);
    const modelInput=$('[data-settings-field="model_enabled"]',settings),providerSelect=$('[data-settings-field="provider_id"]',settings);
    memoryInput.disabled=!memoryReady && !account.memory_enabled;
    modelInput.disabled=!providers.length && !account.model_enabled;
    providerSelect.disabled=!providers.length;
    $('[data-life-provider-note]',settings).textContent=providers.length?'只列出已配置的模型服务商。不会显示密钥或内部配置。':'还没有可用的模型服务商；配置完成后才能启用模型辅助。';
    $('[data-life-memory-note]',settings).textContent=memoryReady?'生活记忆组件已就绪。':'生活记忆组件未安装或尚未就绪；可以先关闭同步，已有记录会保留。';
    renderEvents(account,node);
  }
  function renderEvents(account,node) {
    const accountKey=accountId(account),items=overview.events.filter(event=>String(event.account_id || '')===accountKey)
      .sort((a,b)=>String(b.occurred_at || '').localeCompare(String(a.occurred_at || ''))).slice(0,5);
    const signature=JSON.stringify(items);
    const host=$('[data-life-events]',node);
    if (host.dataset.signature===signature) return;
    host.dataset.signature=signature;
    host.innerHTML=`<h4>近期生活事件</h4>${items.length?`<ul>${items.map(event=>`<li><span><strong>${esc(event.name || event.description || '生活事件')}</strong><small>${esc(formatTime(event.occurred_at))}</small></span><span class="life-event-memory">${esc(memoryLabels[event.memory_status] || event.memory_status || event.status || '已完成')}</span></li>`).join('')}</ul>`:'<p class="help">还没有生活事件记录。</p>'}`;
  }
  function renderAccounts() {
    const accounts=overview.accounts;
    $('#lifeAccountCount').textContent=String(accounts.length);
    $('#lifeNoAccounts').hidden=accounts.length>0;
    const list=$('#lifeAccountList');
    const existing=new Map($$('.life-account',list).map(node=>[node.dataset.accountId,node]));
    const ordered=[];
    for (const account of accounts) {
      const id=accountId(account);
      let node=existing.get(id);
      if (!node) node=createAccountCard(account);
      renderLiveAccount(account,node);
      ordered.push(node);
    }
    let cursor=list.firstElementChild;
    for (const node of ordered) {
      if (node===cursor) cursor=cursor.nextElementSibling;
      else list.insertBefore(node,cursor);
    }
    const keep=new Set(ordered);
    for (const node of [...list.children]) if (!keep.has(node)) node.remove();
    list.querySelectorAll('.life-avatar img').forEach(image=>{
      if (image.dataset.fallbackReady) return;
      image.dataset.fallbackReady='true';
      image.addEventListener('error',()=>{image.hidden=true;if(image.nextElementSibling)image.nextElementSibling.hidden=false;},{once:true});
    });
  }

  function stateOptions(selected) {
    const states=stateCatalog(),exists=states.some(item=>item.id===selected);
    return `<option value="">选择状态</option>${selected && !exists?`<option value="${esc(selected)}" selected>当前设置的状态</option>`:''}${states.map(item=>`<option value="${esc(item.id)}" ${item.id===selected?'selected':''}>${esc(item.name || '未命名状态')}</option>`).join('')}`;
  }
  function operatorOptions(selected,kind) {
    const options=kind==='condition'?conditionOps:effectOps;
    const extra=selected && !options.some(([value])=>value===selected)?[[selected,selected]]:[];
    return [...options,...extra].map(([value,label])=>`<option value="${esc(value)}" ${selected===value?'selected':''}>${esc(label)}</option>`).join('');
  }
  function roomOptions(selected) {
    const rooms=currentRooms(),exists=rooms.some(item=>item.id===selected);
    return `<option value="">不指定房间</option>${selected && !exists?`<option value="${esc(selected)}" selected>当前设置的房间</option>`:''}${rooms.map(room=>`<option value="${esc(room.id)}" ${room.id===selected?'selected':''}>${esc(roomDisplayName(room))}</option>`).join('')}`;
  }
  function valueKind(stateId) {return stateById(stateId)?.type || 'number';}
  function clauseRow(clause={},kind='condition') {
    const state=String(clause.state || ''),op=String(clause.op || (kind==='condition'?'gte':'add'));
    return `<div class="life-clause-row" data-${kind}-row><label>${kind==='condition'?'状态条件':'状态' }<select data-clause-field="state">${stateOptions(state)}</select></label><label>${kind==='condition'?'判断方式':'变化方式'}<select data-clause-field="op">${operatorOptions(op,kind)}</select></label><label>${kind==='condition'?'比较值':'数值'}<input data-clause-field="value" type="${valueKind(state)==='number'?'number':'text'}" step="any" value="${esc(clause.value ?? '')}"></label><button type="button" class="quiet small" data-life-remove-row aria-label="移除此${kind==='condition'?'条件':'效果'}">移除</button></div>`;
  }
  function rateRow(state,value) {
    return `<div class="life-clause-row" data-rate-row><label>影响状态<select data-rate-field="state">${stateOptions(state)}</select></label><label>每小时变化<input data-rate-field="value" type="number" step="any" value="${esc(value ?? 0)}"></label><button type="button" class="quiet small" data-life-remove-row aria-label="移除每小时变化">移除</button></div>`;
  }
  function normalizedActionPresetIds(action) {
    const ids=Array.isArray(action?.preset_ids)?action.preset_ids:action?.preset_id?[action.preset_id]:[];
    return ids.length?[DEFAULT_PRESET_ID]:[];
  }
  function windowRow(window={}) {
    const hasDays=Array.isArray(window.days),days=hasDays?window.days.map(Number):[0,1,2,3,4,5,6],labels=['一','二','三','四','五','六','日'];
    return `<div class="life-window-row" data-window-row data-days-mode="${hasDays?'custom':'default'}"><div class="life-window-times"><label>开始<input data-window-field="start" type="time" value="${esc(window.start || '')}"></label><label>结束<input data-window-field="end" type="time" value="${esc(window.end || '')}"></label></div><fieldset class="life-window-days" data-window-days><legend>重复星期</legend>${labels.map((label,index)=>`<label><input type="checkbox" value="${index}" ${days.includes(index)?'checked':''}>周${label}</label>`).join('')}</fieldset><button type="button" class="quiet small" data-life-remove-row aria-label="移除此时间段">移除</button></div>`;
  }
  function stepMarkup(step={},index) {
    const rates=Object.entries(step.rates || {}),effects=step.effects || [];
    return `<section class="life-step" data-step-row data-step-index="${index}">
      <div class="life-step-heading"><h5>阶段 ${index+1}</h5><button type="button" class="quiet small" data-life-remove-step ${index===0?'disabled':''}>移除阶段</button></div>
      <div class="life-step-fields"><label>阶段名称<input data-step-field="name" value="${esc(step.name || '')}" placeholder="例如：专心工作"></label><label>目标房间<select data-step-field="room_id">${roomOptions(step.room_id || '')}</select></label><label>持续时间（分钟）<input data-step-field="duration_minutes" type="number" min="1" step="1" value="${esc(step.duration_minutes ?? 1)}"></label></div>
      <div class="life-action-subsection"><div class="life-action-subheading"><strong>阶段期间</strong><span>持续影响状态</span></div><div class="life-rate-rows">${rates.map(([state,value])=>rateRow(state,value)).join('') || '<p class="help">没有持续状态变化。</p>'}</div><button type="button" class="quiet small" data-life-add="rate">添加持续变化</button></div>
      <div class="life-action-subsection"><div class="life-action-subheading"><strong>阶段完成后</strong><span>增加填正数，减少填负数；“设为”会保留负数目标值。</span></div><div class="life-effect-rows">${effects.map(effect=>clauseRow(effect,'effect')).join('') || '<p class="help">没有完成效果。</p>'}</div><button type="button" class="quiet small" data-life-add="effect">添加完成效果</button></div>
    </section>`;
  }
  function renderActionForm(action) {
    const windows=action.windows || [],conditions=action.conditions || [],steps=action.steps || [];
    return `<label>动作名称<input data-action-field="name" maxlength="120" value="${esc(action.name || '')}"></label>
      <label>说明<textarea data-action-field="description" rows="2" maxlength="500">${esc(action.description || '')}</textarea></label>
      <label>分类<select data-action-field="category" required>${actionCategoryOptions(String(action.category || ''))}</select></label>
      <div class="life-action-fields"><label class="life-inline-check"><input data-action-field="enabled" type="checkbox" ${action.enabled?'checked':''}><span>启用</span></label><label>优先级<input data-action-field="priority" type="number" step="1" value="${esc(action.priority ?? 0)}"></label><label>冷却时间（分钟）<input data-action-field="cooldown_minutes" type="number" min="0" step="1" value="${esc(action.cooldown_minutes ?? 0)}"></label></div>
      <section class="life-action-subsection"><div class="life-action-subheading"><strong>可执行时段</strong><span>留空表示不限时段</span></div><div class="life-window-rows">${windows.map(windowRow).join('') || '<p class="help">没有额外时间限制。</p>'}</div><button type="button" class="quiet small" data-life-add="window">添加时段</button></section>
      <section class="life-action-subsection"><div class="life-action-subheading"><strong>开始条件</strong><span>满足的状态才会执行</span></div><div class="life-condition-rows">${conditions.map(condition=>clauseRow(condition,'condition')).join('') || '<p class="help">没有额外条件。</p>'}</div><button type="button" class="quiet small" data-life-add="condition">添加条件</button></section>
      <section class="life-action-subsection"><div class="life-action-subheading"><strong>执行步骤</strong><span>按顺序进入每个房间和阶段</span></div><div class="life-step-list">${steps.map(stepMarkup).join('') || '<p class="help">这个动作还没有执行阶段。</p>'}</div><button type="button" class="quiet small" data-life-add="step">添加阶段</button></section>
      <div class="life-action-error" data-action-error hidden></div><footer class="life-action-footer"><span>保存后只更新这个动作。</span><button type="submit" class="secondary" data-save-action>保存动作</button></footer>`;
  }
  function actionSummary(action) {
    const rooms=currentRooms(),roomNames=[...new Set((action.steps || []).map(step=>{const room=rooms.find(item=>item.id===step.room_id);return room?roomDisplayName(room):step.room_name;}).filter(Boolean))];
    const roomText=roomNames.length?roomNames.slice(0,2).join('、')+(roomNames.length>2?`等 ${roomNames.length} 个房间`:''):'未指定房间';
    const minutes=(action.steps || []).reduce((total,step)=>total+Math.max(0,Number(step.duration_minutes) || 0),0);
    const timeText=minutes>=60?`${Math.floor(minutes/60)} 小时${minutes%60?` ${minutes%60} 分钟`:''}`:`${minutes} 分钟`;
    const dayNames=['周一','周二','周三','周四','周五','周六','周日'];
    const windows=(action.windows || []).map(window=>{
      const days=Array.isArray(window.days)?window.days.map(Number).sort((a,b)=>a-b):[];
      const dayText=days.length===7?'每天':JSON.stringify(days)===JSON.stringify([0,1,2,3,4])?'工作日':JSON.stringify(days)===JSON.stringify([5,6])?'周末':days.length?days.map(day=>dayNames[day] || '').filter(Boolean).join('、'):'不限日期';
      return `${dayText} ${window.start || '—'}–${window.end || '—'}`;
    });
    return `${action.category || '未分类'} · ${action.enabled?'启用':'停用'} · ${roomText} · ${timeText} · ${windows.join(' / ') || '不限时段'}`;
  }
  function renderActionCategoryFilters() {
    const host=$('#lifeActionCategories'),categories=[...new Set(overview.actions.map(action=>String(action.category || '').trim() || '未分类'))];
    if(actionCategoryFilter!=='*' && !categories.includes(actionCategoryFilter))actionCategoryFilter='*';
    const html=[['*','全部'],...categories.map(name=>[name,name])].map(([value,label])=>`<button type="button" class="${actionCategoryFilter===value?'active':''}" data-life-category-filter="${esc(value)}">${esc(label)}</button>`).join('');
    if(host.dataset.renderMarkup!==html){host.innerHTML=html;host.dataset.renderMarkup=html;}
  }
  function applyActionFilter() {
    const host=$('#lifeActionList');
    for(const section of $$('.life-action-category',host)){
      const items=$$('.life-action-item',section);for(const item of items)item.hidden=actionCategoryFilter!=='*' && item.dataset.category!==actionCategoryFilter;
      section.hidden=!items.some(item=>!item.hidden);
    }
  }
  function renderActions(force=false) {
    const actions=overview.actions,indoor=getData().indoor || {},signature=JSON.stringify([actions,actionCategoryFilter,(indoor.groups || []).map(group=>[group.id,group.name,group.parent_id]),(indoor.rooms || []).map(room=>[room.id,room.name,room.group_id])]);
    if (!force && signature===actionMarkupKey) return;
    const dirty=$$('.life-action-form',panel).some(form=>form.dataset.dirty==='true');
    if (!force && dirty) {applyActionFilter();return;}
    const host=$('#lifeActionList');
    if (!actions.length) {
      host.innerHTML='<p class="empty">预设还没有可编辑的生活动作。</p>';
      actionMarkupKey=signature;return;
    }
    const open=new Set($$('.life-action-item[open]',host).map(node=>node.dataset.actionItem)),categories=[...new Set(actions.map(action=>String(action.category || '').trim() || '未分类'))];
    host.innerHTML=categories.map(category=>`<section class="life-action-category" data-life-category="${esc(category)}"><h3>${esc(category)} <span class="count">${actions.filter(action=>(String(action.category || '').trim() || '未分类')===category).length}</span></h3>${actions.filter(action=>(String(action.category || '').trim() || '未分类')===category).map((action,index)=>`<details class="life-action-item" data-action-item="${esc(action.id)}" data-category="${esc(String(action.category || '').trim() || '未分类')}" ${open.has(String(action.id))?'open':''}><summary><span class="life-action-name">${esc(action.name || `动作 ${index+1}`)}</span><small>${esc(actionSummary(action))}</small></summary><form class="life-action-form" data-action-id="${esc(action.id)}" novalidate>${renderActionForm(action)}</form></details>`).join('')}</section>`).join('');
    applyActionFilter();
    actionMarkupKey=signature;
  }
  function render() {
    renderContents();renderAccounts();renderActionCategoryFilters();renderActions();
  }
  function currentFormAction(form) {
    const base=overview.actions.find(item=>String(item.id)===form.dataset.actionId);
    if (!base) throw Error('这个动作已经不存在，请同步后重试。');
    const value=(selector,root=form)=>$(selector,root)?.value ?? '';
    const valueFromState=(stateId,raw)=>{
      const type=valueKind(stateId);
      if (type==='number') return Number(raw || 0);
      if (type==='boolean') return raw==='true';
      return raw;
    };
    const collectClause=(node,kind)=>{
      const state=$('[data-clause-field="state"]',node).value,op=$('[data-clause-field="op"]',node).value,raw=$('[data-clause-field="value"]',node).value;
      return state?{state,op,value:valueFromState(state,raw)}:null;
    };
    const action={...copy(base),name:value('[data-action-field="name"]').trim(),description:value('[data-action-field="description"]').trim(),category:value('[data-action-field="category"]').trim(),preset_ids:normalizedActionPresetIds(base),enabled:$('[data-action-field="enabled"]',form).checked,
      priority:Number(value('[data-action-field="priority"]') || 0),cooldown_minutes:Number(value('[data-action-field="cooldown_minutes"]') || 0),
      windows:$$('[data-window-row]',form).map(node=>{
        const item={start:$('[data-window-field="start"]',node).value,end:$('[data-window-field="end"]',node).value};
        if (node.dataset.daysMode!=='default') item.days=$$('[data-window-days] input:checked',node).map(input=>Number(input.value));
        return item;
      }).filter(item=>item.start || item.end),
      conditions:$$('[data-condition-row]',form).map(node=>collectClause(node,'condition')).filter(Boolean),
      steps:$$('[data-step-row]',form).map(node=>{
        const rates={};
        for (const row of $$('[data-rate-row]',node)) {const state=$('[data-rate-field="state"]',row).value;if(state)rates[state]=Number($('[data-rate-field="value"]',row).value || 0);}
        const effects=$$('[data-effect-row]',node).map(row=>collectClause(row,'effect')).filter(Boolean);
        return {name:$('[data-step-field="name"]',node).value.trim(),room_id:$('[data-step-field="room_id"]',node).value,duration_minutes:Number($('[data-step-field="duration_minutes"]',node).value || 0),effects,rates};
      })
    };
    return action;
  }
  function validateAction(action) {
    if (!action.name) return '请填写动作名称。';
    if (!action.category) return '请选择生活动作分类。';
    if (action.priority<0 || !Number.isFinite(action.priority)) return '优先级需为不小于 0 的数字。';
    if (action.cooldown_minutes<0 || !Number.isFinite(action.cooldown_minutes)) return '冷却时间需为不小于 0 的分钟数。';
    if (action.windows.some(item=>!item.start || !item.end)) return '请补全每个时段的开始和结束时间，或移除空时段。';
    if (action.windows.some(item=>Array.isArray(item.days) && !item.days.length)) return '每个时段至少选择一个重复星期。';
    if (!action.steps.length) return '至少保留一个执行阶段。';
    if (action.steps.some(step=>!step.name || !Number.isInteger(step.duration_minutes) || step.duration_minutes<1)) return '请填写每个阶段名称和至少 1 分钟的整数时长。';
    if (action.steps.some(step=>Object.values(step.rates).some(value=>!Number.isFinite(value)))) return '每小时变化需要填写有效数字。';
    return '';
  }
  async function saveAction(form) {
    const action=currentFormAction(form),error=validateAction(action),errorNode=$('[data-action-error]',form),button=$('[data-save-action]',form);
    if (error) {errorNode.textContent=error;errorNode.hidden=false;return;}
    errorNode.hidden=true;
    await run(button,async()=>{
      try {
        const result=await api('life/actions/save',{action},null);
        const next=unwrapLife(result);
        if (next) {overview={...next,options:{personas:[],providers:[],...(next.options || {})}};overview.options.personas=[...(overview.options.personas || [])];overview.options.providers=[...(overview.options.providers || [])];}
        else {
          overview.actions=overview.actions.map(item=>item.id===action.id?action:item);
        }
        form.dataset.dirty='false';actionMarkupKey=JSON.stringify(overview.actions);onChange?.(overview);render();toast('动作已保存');
      } catch (failure) {
        errorNode.textContent=failure.message || '动作保存失败，请检查后重试。';errorNode.hidden=false;throw failure;
      }
    }).catch(()=>{});
  }
  async function toggleAccount(button) {
    const id=button.closest('.life-account')?.dataset.accountId,account=overview.accounts.find(item=>accountId(item)===id);
    if (!account || pendingAccounts.has(id)) return;
    const nextEnabled=!enabled(account),personaId=String($('[data-settings-field="persona_id"]',button.closest('.life-account'))?.value || '');
    pendingAccounts.add(id);button.disabled=true;
    try {
      const result=await api('life/preset',{preset_id:DEFAULT_PRESET_ID,enabled:nextEnabled,account_id:id,persona_id:personaId},null);
      applyResult(result);
      toast(nextEnabled?'已为此 QQ 启用自动生活':'自动生活已暂停，已有设置和记录仍保留');
    } catch (error) {
      const node=$('[data-life-settings-error]',button.closest('.life-account'));
      node.textContent=error.message || '生活预设设置失败，请重试。';node.hidden=false;
      toast(error.message || '生活预设设置失败，请重试。',true);
    } finally {pendingAccounts.delete(id);button.disabled=false;renderAccounts();}
  }
  async function saveAccountSettings(form) {
    const node=form.closest('.life-account'),id=node?.dataset.accountId,account=overview.accounts.find(item=>accountId(item)===id);
    if (!account) return;
    const personaId=$('[data-settings-field="persona_id"]',form).value;
    const settings={memory_enabled:$('[data-settings-field="memory_enabled"]',form).checked,model_enabled:$('[data-settings-field="model_enabled"]',form).checked,provider_id:$('[data-settings-field="provider_id"]',form).value};
    const personaChanged=personaId!==String(account.persona_id || ''),settingsChanged=settings.memory_enabled!==Boolean(account.memory_enabled) || settings.model_enabled!==Boolean(account.model_enabled) || settings.provider_id!==String(account.provider_id || '');
    const errorNode=$('[data-life-settings-error]',form),button=$('[data-life-save-settings]',form);
    if (!personaChanged && !settingsChanged) {toast('账号设置没有变化。');return;}
    errorNode.hidden=true;pendingAccounts.add(id);button.disabled=true;
    try {
      let result=null;
      if (settingsChanged) result=await api('life/settings',{account_id:id,...settings},null);
      if (personaChanged) result=await api('life/preset',{preset_id:DEFAULT_PRESET_ID,enabled:enabled(account),account_id:id,persona_id:personaId},null);
      const next=unwrapLife(result);
      if (next) {
        overview={...next,options:{personas:[],providers:[],...(next.options || {})}};
        overview.options.personas=[...(overview.options.personas || [])];overview.options.providers=[...(overview.options.providers || [])];
      } else {
        overview.accounts=overview.accounts.map(item=>accountId(item)===id?{...item,persona_id:personaChanged?personaId:item.persona_id,...(settingsChanged?settings:{})}:item);
      }
      settingsDrafts.delete(id);onChange?.(overview);render();toast('QQ 账号设置已保存');
    } catch (error) {
      errorNode.textContent=error.message || '账号设置保存失败，请重试。';errorNode.hidden=false;
      toast(error.message || '账号设置保存失败，请重试。',true);
    } finally {pendingAccounts.delete(id);button.disabled=false;renderAccounts();}
  }
  function readActionForChange(form) {
    try {return currentFormAction(form);} catch {return null;}
  }
  function replaceActionForm(form,action,focusSelector='') {
    form.innerHTML=renderActionForm(action);form.dataset.dirty='true';
    const target=focusSelector?$(focusSelector,form):null;
    target?.focus({preventScroll:true});
  }

  panel.addEventListener('input',event=>{
    const accountForm=event.target.closest('[data-life-settings]');
    if (accountForm) {
      const card=accountForm.closest('.life-account'),id=card.dataset.accountId,account=overview.accounts.find(item=>accountId(item)===id);
      if (!account) return;
      let draft=settingsDrafts.get(id) || {persona_id:String(account.persona_id || ''),settings:{memory_enabled:Boolean(account.memory_enabled),model_enabled:Boolean(account.model_enabled),provider_id:String(account.provider_id || '')}};
      draft.persona_id=$('[data-settings-field="persona_id"]',accountForm).value;
      draft.settings={memory_enabled:$('[data-settings-field="memory_enabled"]',accountForm).checked,model_enabled:$('[data-settings-field="model_enabled"]',accountForm).checked,provider_id:$('[data-settings-field="provider_id"]',accountForm).value};
      draft.dirty=true;settingsDrafts.set(id,draft);return;
    }
    const actionForm=event.target.closest('.life-action-form');
    if (actionForm) {actionForm.dataset.dirty='true';const error=$('[data-action-error]',actionForm);if(error)error.hidden=true;}
  });
  panel.addEventListener('change',event=>{
    if (event.target.matches('[data-settings-field]')) {
      event.target.dispatchEvent(new Event('input',{bubbles:true}));
      return;
    }
    if (event.target.matches('[data-clause-field="state"]')) {
      const row=event.target.closest('.life-clause-row'),value=$('[data-clause-field="value"]',row);
      value.type=valueKind(event.target.value)==='number'?'number':'text';
    }
    const windowDays=event.target.closest('[data-window-days]');
    if (windowDays) windowDays.closest('[data-window-row]').dataset.daysMode='custom';
    const actionForm=event.target.closest('.life-action-form');
    if (actionForm) {actionForm.dataset.dirty='true';const error=$('[data-action-error]',actionForm);if(error)error.hidden=true;}
  });
  panel.addEventListener('click',event=>{
    const categoryFilter=event.target.closest('[data-life-category-filter]');
    if(categoryFilter){actionCategoryFilter=categoryFilter.dataset.lifeCategoryFilter;renderActionCategoryFilters();renderActions();return;}
    const toggle=event.target.closest('[data-life-toggle]');
    if (toggle) {void toggleAccount(toggle);return;}
    const add=event.target.closest('[data-life-add]');
    if (add) {
      const form=add.closest('.life-action-form'),action=form && readActionForChange(form);
      if (!form || !action) return;
      const type=add.dataset.lifeAdd;
      if (type==='window') action.windows.push({start:'',end:'',days:[0,1,2,3,4,5,6]});
      if (type==='condition') action.conditions.push({state:'',op:'gte',value:0});
      if (type==='step') action.steps.push({name:'',room_id:'',duration_minutes:1,effects:[],rates:{}});
      if (type==='effect') action.steps[Number(add.closest('[data-step-row]').dataset.stepIndex)]?.effects.push({state:'',op:'add',value:0});
      if (type==='rate') {
        const step=action.steps[Number(add.closest('[data-step-row]').dataset.stepIndex)];
        if (step) {step.rates ||= {};step.rates[''] ||= 0;}
      }
      replaceActionForm(form,action,type==='condition'?'.life-condition-rows select':type==='step'?'.life-step-list [data-step-field="name"]':type==='effect'?'.life-effect-rows select':type==='rate'?'.life-rate-rows select':'.life-window-rows input');
      return;
    }
    const remove=event.target.closest('[data-life-remove-row]');
    if (remove) {
      const form=remove.closest('.life-action-form'),action=form && readActionForChange(form),row=remove.closest('[data-window-row],[data-condition-row],[data-effect-row],[data-rate-row]');
      if (!form || !action || !row) return;
      const stepNode=row.closest('[data-step-row]'),step=stepNode && action.steps[Number(stepNode.dataset.stepIndex)];
      if (row.hasAttribute('data-window-row')) action.windows.splice($$('[data-window-row]',form).indexOf(row),1);
      else if (row.hasAttribute('data-condition-row')) action.conditions.splice($$('[data-condition-row]',form).indexOf(row),1);
      else if (row.hasAttribute('data-effect-row')) step?.effects.splice($$('[data-effect-row]',stepNode).indexOf(row),1);
      else if (row.hasAttribute('data-rate-row') && step) delete step.rates[$('[data-rate-field="state"]',row).value];
      replaceActionForm(form,action);return;
    }
    const removeStep=event.target.closest('[data-life-remove-step]');
    if (removeStep && !removeStep.disabled) {
      const form=removeStep.closest('.life-action-form'),action=form && readActionForChange(form),step=removeStep.closest('[data-step-row]');
      if (form && action && step) {action.steps.splice(Number(step.dataset.stepIndex),1);replaceActionForm(form,action);}
    }
  });
  panel.addEventListener('submit',event=>{
    const settings=event.target.closest('[data-life-settings]');
    if (settings) {event.preventDefault();void saveAccountSettings(settings);return;}
    const action=event.target.closest('.life-action-form');
    if (action) {event.preventDefault();void saveAction(action);}
  });

  return {
    accept,
    loadFailed(error) {showPanelError(`生活预设未能加载：${error?.message || '连接暂不可用。'}`);},
  };
}
