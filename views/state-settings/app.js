import {mountSchedule} from './schedule.js';
import {mountIndoor} from './indoor.js';
import {mountGroupEditor} from './group-editor.js';
import {mountLife} from './life.js';
import {mountSharing} from './sharing.js';
import {closeDialog,installDetails,setDetailsOpen,setExpanded,showDialog,switchPanels} from './motion.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const kindNames = {number:'数值', boolean:'布尔', enum:'枚举', text:'文本'};
const scopeNames = {character:'角色整体', conversation:'当前会话', user:'与用户的关系'};
const blank = () => ({id:'',name:'',description:'',category:'',type:'number',scope:'character',initial:0,min:0,max:100,rate_per_hour:0,options:[],enabled:true,inject:true,show_value:true,prompt:'{name}：{value}；{stage}。{description}',stages:[],interactions:[],events:[],editor_blocks:[]});
const emptySnapshot = {definitions:[],groups:[],states:[],categories:[],contexts:[],actors:[],accounts:[],schedule:{timezone:'Asia/Shanghai',entries:[]},schedule_runtime:{local_time:null,active:null,next:null},indoor:{rooms:[],connections:[],initial_room_id:''},indoor_runtime:{room_id:'',room:null,exits:[],occupancy:{},occupants:[]},life:{presets:[],accounts:[],actions:[],events:[],options:{personas:[],providers:[]},integrations:{livingmemory:{installed:false,ready:false,version:''}}}};
let bridge, data = structuredClone(emptySnapshot), scheduleView, indoorView, groupEditor, lifeView, sharingView;
let actor = null;
let selectedLifeAccountId = '';
let draft = blank(), editing = false, builderMode = 'state', categoryFilter = '*', selectedStateKey = '', toastTimer;
let bootstrapPromise = null, pendingContextRequest = null, contextDrainPromise = null;
let writeCount = 0, snapshotVersion = 0, pollTimer = 0, filterMarkup = '';
installDetails(document);

function toast(message, error = false) {
  const node = $('#toast'); node.textContent = message; node.classList.toggle('error', error); node.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => node.hidden = true, 4000);
}
async function api(endpoint, body, contextOverride) {
  const requestContext = contextOverride !== undefined ? contextOverride : actor;
  let result;
  if (body === undefined) {
    const selection=endpoint==='bootstrap' && contextOverride===undefined && selectedLifeAccountId ? {account_id:selectedLifeAccountId} : requestContext;
    result = await bridge.apiGet(endpoint, selection || {});
  }
  else {
    writeCount++; snapshotVersion++;
    try {
      result = await bridge.apiPost(endpoint, {
        ...body,
        ...(!Object.prototype.hasOwnProperty.call(body,'context') && !Object.prototype.hasOwnProperty.call(body,'account_id') && requestContext ? {context:requestContext} : {})
      });
    } finally { writeCount--; snapshotVersion++; }
  }
  if (result?.status === 'error') throw Error(result.message || '操作失败');
  return result?.status === 'ok' && result.data !== undefined ? result.data : result;
}
function setSyncStatus(message,failed = false) {
  $('#syncTime').textContent = message;
  $('#retrySyncButton').hidden = !failed;
  $('#syncTime').classList.toggle('is-error',failed);
}
function accept(result) {
  const incoming = result || {};
  data = {...emptySnapshot,...incoming,
    schedule:{...emptySnapshot.schedule,...(incoming.schedule || {})},
    schedule_runtime:{...emptySnapshot.schedule_runtime,...(incoming.schedule_runtime || {})},
    indoor:{...emptySnapshot.indoor,...(incoming.indoor || {}),rooms:[...(incoming.indoor?.rooms || [])],connections:[...(incoming.indoor?.connections || [])]},
    indoor_runtime:{...emptySnapshot.indoor_runtime,...(incoming.indoor_runtime || {}),exits:[...(incoming.indoor_runtime?.exits || [])],occupancy:{...(incoming.indoor_runtime?.occupancy || {})},occupants:[...(incoming.indoor_runtime?.occupants || [])]},
    life:{...emptySnapshot.life,...(incoming.life || {}),presets:[...(incoming.life?.presets || [])],accounts:[...(incoming.life?.accounts || [])],actions:[...(incoming.life?.actions || [])],events:[...(incoming.life?.events || [])],options:{...emptySnapshot.life.options,...(incoming.life?.options || {})},integrations:{...emptySnapshot.life.integrations,...(incoming.life?.integrations || {})}},
    contexts:[...(incoming.contexts || [])],actors:[...(incoming.actors || [])],accounts:[...(incoming.accounts || [])],groups:[...(incoming.groups || [])],states:[...(incoming.states || [])]
  };
  if (Object.prototype.hasOwnProperty.call(incoming,'context')) actor = incoming.context || null;
  $('#connectionError').hidden = true;
  $('#stateCount').textContent = data.states.length;
  setSyncStatus('最近同步 ' + new Date().toLocaleTimeString('zh-CN'));
  renderStates(); groupEditor?.setData(data); scheduleView?.render(); indoorView?.accept(data); lifeView?.accept(data.life); sharingView?.acceptStatus(incoming.sharing,data.accounts,data.life.accounts);
}
async function run(button, fn) {
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try { return await fn(); } catch (error) { toast(error.message, true); throw error; }
  finally { if (button) button.disabled = false; }
}
async function readBootstrap(contextOverride,options = {}) {
  const {poll=false,version=snapshotVersion}=options;
  if (bootstrapPromise) return bootstrapPromise;
  bootstrapPromise = (async () => {
    try {
      const incoming = await api('bootstrap',undefined,contextOverride);
      if (version !== snapshotVersion || writeCount || (poll && document.visibilityState !== 'visible')) return null;
      accept(incoming);
      return incoming;
    } catch (error) {
      if (!poll) throw error;
      setSyncStatus(`同步失败 · ${error.message || '连接暂不可用'}`,true);
      return null;
    } finally { bootstrapPromise = null; }
  })();
  return bootstrapPromise;
}
async function drainContextQueue() {
  while (pendingContextRequest) {
    const request=pendingContextRequest;pendingContextRequest=null;
    if (bootstrapPromise) await bootstrapPromise.catch(() => {});
    while (writeCount) await new Promise(resolve => setTimeout(resolve,20));
    if (pendingContextRequest) {request.resolve(null);continue;}
    request.version=snapshotVersion;
    try {request.resolve(await readBootstrap(request.context,{version:request.version}));}
    catch (error) {request.reject(error);}
  }
  contextDrainPromise=null;
  if (pendingContextRequest) contextDrainPromise=drainContextQueue();
}
function queueContextRefresh(context) {
  snapshotVersion++;
  const version=snapshotVersion;
  const promise=new Promise((resolve,reject) => {
    if (pendingContextRequest) pendingContextRequest.resolve(null);
    pendingContextRequest={context,version,resolve,reject};
  });
  if (!contextDrainPromise) contextDrainPromise=drainContextQueue();
  return promise;
}
function refresh(contextOverride,{poll=false} = {}) {
  if (contextOverride !== undefined) return queueContextRefresh(contextOverride);
  if (poll && (document.visibilityState !== 'visible' || writeCount || pendingContextRequest || contextDrainPromise)) return Promise.resolve(null);
  if (!poll && contextDrainPromise) return contextDrainPromise.then(() => null);
  if (poll && bootstrapPromise) return bootstrapPromise;
  return readBootstrap(undefined,{poll,version:snapshotVersion});
}
function startPolling(immediate = false) {
  clearInterval(pollTimer);
  if (document.visibilityState !== 'visible') return;
  if (immediate) refresh(undefined,{poll:true}).catch(() => {});
  pollTimer = setInterval(() => { if (document.visibilityState === 'visible') refresh(undefined,{poll:true}).catch(() => {}); },2000);
}
function mainTab(which) {
  const selected = $(`[data-main-tab="${which}"]`);
  if (!selected) return;
  for (const node of $$('[data-main-tab]')) {
    const active = node === selected;
    node.setAttribute('aria-selected', String(active));
    node.tabIndex = active ? 0 : -1;
  }
  switchPanels($$('[data-main-panel]'),which);
  revealMainTab(selected);
  if (which === 'environment') indoorView?.onMainTab();
}
function revealMainTab(button) {
  const reduceMotion=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  button.scrollIntoView({behavior:reduceMotion?'auto':'smooth',block:'nearest',inline:'nearest'});
}
function tab(which,focus = false) {
  const viewing = which === 'view';
  for (const [node, selected] of [[$('#viewTab'),viewing],[$('#createTab'),!viewing]]) {
    node.setAttribute('aria-selected', String(selected)); node.tabIndex = selected ? 0 : -1;
  }
  switchPanels([$('#viewPanel'),$('#createPanel')],viewing?'viewPanel':'createPanel');
  if (focus) $(viewing?'#viewTab':'#createTab').focus();
}
function renderStates() {
  if (categoryFilter !== '*' && categoryFilter && !data.categories.some(c => c.id === categoryFilter)) categoryFilter = '*';
  const categories = [...data.categories,{id:'',name:'未分类'}];
  const filterHTML = [{id:'*',name:'全部'},...categories].map(c => `<button class="${categoryFilter === c.id?'active':''}" data-action="filter" data-category="${esc(c.id)}">${esc(c.name)}</button>`).join('');
  if (filterHTML !== filterMarkup) { $('#categoryFilters').innerHTML = filterHTML; filterMarkup = filterHTML; }
  if (selectedStateKey && !data.states.some(row => row.key === selectedStateKey)) selectedStateKey = '';
  $('#stateCount').textContent = data.states.length;
  const definitions = [
    ...data.definitions.map(definition => ({...definition,kind:'state'})),
    ...data.groups.map(definition => ({...definition,kind:'group',type:'number',computed:true}))
  ];
  const parent = $('#stateGroups');
  if (!definitions.length && !data.categories.length) {
    const emptyHTML = '<div class="empty"><p>暂无基础状态或综合指标</p><button class="primary" data-action="new">新建状态</button></div>';
    if (parent.dataset.renderHTML !== emptyHTML) { parent.innerHTML=emptyHTML; parent.dataset.renderHTML=emptyHTML; }
    return;
  }
  parent.dataset.renderHTML='';
  const current = new Map($$('.category',parent).map(section => [section.dataset.category,section]));
  const visibleCategories = categories.filter(category => categoryFilter === '*' || categoryFilter === category.id);
  const orderedSections=[];
  let rowIndex=0;
  for (const category of visibleCategories) {
    const categoryId=category.id || '';
    let section=current.get(categoryId);
    if (!section) {
      const template=document.createElement('template');
      template.innerHTML=`<section class="category" data-category="${esc(categoryId)}"><div class="category-head"><h3></h3><div class="category-actions"></div></div><div class="state-grid"></div></section>`;
      section=template.content.firstElementChild;
    }
    section.dataset.categoryName=category.name;
    const title=$('h3',section),actions=$('.category-actions',section);
    if (title.textContent !== category.name) title.textContent=category.name;
    const actionHTML=categoryId?`<button class="quiet small" data-action="category-rename" data-category="${esc(categoryId)}">重命名</button><button class="quiet small danger" data-action="category-delete" data-category="${esc(categoryId)}">删除分类</button>`:'';
    if (actions.dataset.renderHTML !== actionHTML) {actions.innerHTML=actionHTML;actions.dataset.renderHTML=actionHTML;}
    const scopedDefinitions=definitions.filter(definition => (definition.category || '')===categoryId);
    const entries=scopedDefinitions.flatMap(definition => data.states.filter(row => row.id===definition.id).map(row => ({definition,row,index:rowIndex++})));
    title.innerHTML=`${esc(category.name)} <span class="count">${entries.length}</span>`;
    const grid=$('.state-grid',section),existing=new Map($$('.state-entry',grid).map(entry => [entry.dataset.stateKey,entry])),orderedEntries=[];
    for (const entry of entries) {
      const html=stateEntry(entry.definition,entry.row,entry.index);
      const node=patchStateEntry(existing.get(entry.row.key),html);
      node.dataset.stateKey=entry.row.key;
      orderedEntries.push(node);
    }
    if (!orderedEntries.length) {
      const empty=document.createElement('div');empty.className='empty small-empty';empty.textContent='此分类还没有基础状态或综合指标。';
      orderedEntries.push(empty);
    }
    commitOrder(grid,orderedEntries);
    orderedSections.push(section);
  }
  commitOrder(parent,orderedSections);
}
function parseNode(html) { const template=document.createElement('template');template.innerHTML=html.trim();return template.content.firstElementChild; }
function commitOrder(parent,nodes) {
  let cursor=parent.firstElementChild;
  for (const node of nodes) {
    if (node===cursor) cursor=cursor.nextElementSibling;
    else parent.insertBefore(node,cursor);
  }
  const keep=new Set(nodes);
  for (const child of [...parent.children]) if (!keep.has(child)) child.remove();
}
function reorderBlocks(parent,order) {
  const byName=new Map($$(':scope > details[data-block]',parent).map(node => [node.dataset.block,node]));
  const active=order.map(name => byName.get(name)).filter(Boolean);
  const inactive=[...byName.entries()].filter(([name]) => !order.includes(name)).map(([,node]) => node);
  for (const node of [...active,...inactive]) parent.append(node);
}
function patchStateEntry(existing,html) {
  const incoming=parseNode(html);
  if (!existing) return incoming;
  existing.className=incoming.className;
  const oldStrip=$('.state-strip',existing),newStrip=$('.state-strip',incoming);
  oldStrip.setAttribute('aria-expanded',newStrip.getAttribute('aria-expanded'));
  oldStrip.setAttribute('aria-controls',newStrip.getAttribute('aria-controls'));
  if (oldStrip.innerHTML!==newStrip.innerHTML) oldStrip.innerHTML=newStrip.innerHTML;
  const oldDetail=$('.state-detail',existing),newDetail=$('.state-detail',incoming);
  oldDetail.id=newDetail.id;
  setExpanded(oldDetail,!newDetail.hidden);
  oldDetail.setAttribute('aria-label',newDetail.getAttribute('aria-label') || '');
  const oldCopy=$('.state-detail-copy',oldDetail),newCopy=$('.state-detail-copy',newDetail);
  if (oldCopy && newCopy && oldCopy.innerHTML!==newCopy.innerHTML) oldCopy.innerHTML=newCopy.innerHTML;
  const oldActions=$('.state-detail-actions',oldDetail),newActions=$('.state-detail-actions',newDetail);
  if (oldActions && newActions) patchDetailActions(oldActions,newActions);
  return existing;
}
function detailActionKey(node) {return node.dataset.action || (node.classList.contains('state-move-category')?'category':'');}
function copyAttributes(target,source) {
  for (const attribute of [...target.attributes]) if (!source.hasAttribute(attribute.name)) target.removeAttribute(attribute.name);
  for (const attribute of [...source.attributes]) if (target.getAttribute(attribute.name)!==attribute.value) target.setAttribute(attribute.name,attribute.value);
  if ('disabled' in target && 'disabled' in source) target.disabled=source.disabled;
}
function patchDetailActions(target,source) {
  const existing=new Map([...target.children].map(node => [detailActionKey(node),node])),ordered=[];
  for (const fresh of [...source.children]) {
    const key=detailActionKey(fresh),current=existing.get(key);
    if (!current || current.tagName!==fresh.tagName) {ordered.push(fresh);continue;}
    copyAttributes(current,fresh);
    if (key==='category') {
      const oldSelect=$('select',current),newSelect=$('select',fresh);
      if (oldSelect && newSelect && oldSelect!==document.activeElement) {
        const value=oldSelect.value,markup=newSelect.innerHTML;
        if (oldSelect.innerHTML!==markup) oldSelect.innerHTML=markup;
        if ([...oldSelect.options].some(option => option.value===value)) oldSelect.value=value;
      }
    } else if (current.textContent!==fresh.textContent) current.textContent=fresh.textContent;
    ordered.push(current);
  }
  commitOrder(target,ordered);
}
function numberText(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return Number(number.toFixed(3)).toLocaleString('zh-CN',{maximumFractionDigits:3});
}
function rawStateValue(definition,value) {
  if (value === null || value === undefined || value === '') return '—';
  if (definition.type === 'boolean') return value ? '是' : '否';
  if (definition.type === 'number') return numberText(value);
  return String(value);
}
function stateValue(definition,row) {
  if (!row.unbound) return rawStateValue(definition,row.value);
  if (definition.kind==='group' || row.kind==='group' || row.computed===true) return `初始预览 ${rawStateValue({type:'number'},row.value)}`;
  return `初始 ${rawStateValue(definition,definition.initial)}`;
}
function makeId(prefix) {
  const token=globalThis.crypto?.randomUUID?.().replaceAll('-','').toLowerCase() || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `${prefix}_${token}`;
}
function inferredBlocks(state) {
  const allowed=['time','stages','interactions','events'];
  const supplied=Array.isArray(state.editor_blocks)?state.editor_blocks.filter((name,index,array) => allowed.includes(name) && array.indexOf(name)===index):[];
  const required=[];
  if (Number(state.rate_per_hour) !== 0) required.push('time');
  if ((state.stages || []).length) required.push('stages');
  if ((state.interactions || []).length) required.push('interactions');
  if ((state.events || []).length) required.push('events');
  return [...supplied,...required.filter(name => !supplied.includes(name))];
}
function showBuilderMode(mode) {
  builderMode=mode;
  const isState=mode==='state';
  $('#baseKindButton').classList.toggle('active',isState);$('#baseKindButton').setAttribute('aria-pressed',String(isState));
  $('#groupKindButton').classList.toggle('active',!isState);$('#groupKindButton').setAttribute('aria-pressed',String(!isState));
  const stateForm=$('#stateForm'),groupForm=$('#groupForm');
  switchPanels([stateForm,groupForm],isState?stateForm:groupForm);
  $('#builderModeHint').textContent=isState?'从名称、含义和初始值开始，再按需添加变化积木。':'选择基础数值状态作为来源，综合指标只读取并计算，不改变来源值。';
  if (!isState) groupEditor?.render();
}
function switchBuilderMode(mode) {
  if (editing || mode===builderMode) return;
  if (builderMode==='state') draft=collect(false);
  else groupEditor?.collect(false);
  if (mode==='group' && !groupEditor?.getDraft()?.id) groupEditor?.newGroup();
  showBuilderMode(mode);
  if (mode==='state') renderEditor(draft,false);
}
function activateStateBlock(block) {
  const node=$(`#stateBuilderBlocks > details[data-block="${block}"]`);
  node.hidden=false;node.dataset.removing='false';void setDetailsOpen(node,true);
  draft=collect(false);
  draft.editor_blocks=activeBuilderBlocks().map(item => item.dataset.block);
  reorderBlocks($('#stateBuilderBlocks'),draft.editor_blocks);
}
function builderBlockNodes() {return $$('#stateBuilderBlocks > details[data-block]');}
function activeBuilderBlocks() {return builderBlockNodes().filter(node => !node.hidden && node.dataset.removing!=='true');}
function isLifeStateRow(row) {
  return Boolean(row?.life || String(row?.actor_id || '').startsWith('life:') || (row?.account_id && !row?.context && !row?.unbound));
}
function lifeOwnerLabel(row) {
  const account=data.life.accounts.find(item => item.id===row.account_id || item.actor_id===row.actor_id);
  const qq=String(account?.qq || row.qq || '').trim();
  const name=String(account?.name || row.name || '').trim();
  return `${name?`${name} · `:''}QQ ${qq || '—'}`;
}
function ownerLabel(definition,row) {
  if (isLifeStateRow(row)) return lifeOwnerLabel(row);
  if (row.owner) return String(row.owner);
  if (row.unbound) return definition.scope === 'conversation' ? '待真实 QQ 会话' : definition.scope === 'user' ? '待真实 QQ 用户' : '';
  const context=row.context || {};
  if (definition.scope === 'user') {
    const qq=String(context.user || '').split(':').filter(Boolean).at(-1);
    return qq ? `QQ ${qq}` : '当前用户';
  }
  if (definition.scope === 'conversation') {
    const parts=String(context.conversation || '').split(':').filter(Boolean);
    const id=parts.at(-1);
    if (!id) return '当前会话';
    const messageType=String(parts.at(-2) || '').toLowerCase();
    return ['group','groupmessage'].includes(messageType) ? `群 ${id}` : ['private','friendmessage'].includes(messageType) ? `私聊 ${id}` : `会话 ${id}`;
  }
  return '';
}
function stateEntry(definition,row,index) {
  const open = row.key === selectedStateKey, value = stateValue(definition,row);
  const isGroup = definition.kind === 'group' || row.kind === 'group' || row.computed === true;
  const lifeRow=isLifeStateRow(row);
  const scope = definition.scope || row.scope_key || 'character';
  const owner = scope === 'character' ? '' : ownerLabel(definition,row);
  const canOperate = !row.unbound && (lifeRow ? Boolean(row.account_id && scope==='character') : Boolean(row.context));
  const stage = row.stage && String(row.stage) !== value ? `<span class="state-stage">${esc(row.stage)}</span>` : '';
  const meterValue = row.unbound && !isGroup ? definition.initial : row.value;
  const bounded = definition.min !== null && definition.min !== '' && definition.max !== null && definition.max !== '' && Number.isFinite(Number(definition.min)) && Number.isFinite(Number(definition.max)) && Number(definition.max) > Number(definition.min);
  const meter = definition.type === 'number' && bounded && Number.isFinite(Number(meterValue))
    ? `<span class="state-meter ${row.unbound?'is-initial':''}" aria-hidden="true"><span style="width:${Math.max(0,Math.min(100,(Number(meterValue)-Number(definition.min))/(Number(definition.max)-Number(definition.min))*100))}%"></span></span>` : '';
  const detailId = `state-detail-${index}`;
  const scopeDetail = scope === 'character' && !lifeRow ? '' : `<p class="state-detail-owner">归属 · ${esc(owner || ownerLabel(definition,row))}${lifeRow?' · 生活全局':''}</p>`;
  const components = (row.components || []).map(component => {
    const share = Number.isFinite(Number(component.weight_percent)) ? `${numberText(component.weight_percent)}%` : `相对权重 ${numberText(component.weight)}`;
    const direction = component.direction === 'negative' ? '反向' : '正向';
    return `<div class="group-component-row"><strong>${esc(component.name || component.state_id || '来源')}</strong><span>原值 ${esc(rawStateValue({type:'number'},component.value))}</span><span>评分 ${esc(numberText(component.score))}</span><span>占比 ${esc(share)}</span><span>贡献 ${esc(numberText(component.contribution))}</span><small>${esc(direction)} · 范围 ${esc(numberText(component.low))}–${esc(numberText(component.high))}</small></div>`;
  }).join('');
  const info = isGroup
    ? `<p><span>综合结果</span>${esc(value)} / 100 · 只读计算</p><p><span>说明</span>${esc(definition.description || '尚未填写描述。')}</p>${scopeDetail}<div class="group-component-list"><strong class="group-component-title">来源与贡献</strong>${components || '<p class="help">暂无来源明细。</p>'}</div>`
    : row.unbound
    ? `<p><span>初始值</span>${esc(rawStateValue(definition,definition.initial))}</p><p><span>说明</span>${esc(definition.description || '尚未填写描述。')}</p>${scopeDetail}<p class="state-unbound-note">等待真实 QQ 会话首次使用。出现真实归属后，运行值才能调整、重置或执行交互。</p>`
    : `<p><span>阶段</span>${esc(row.stage || '—')}</p><p><span>说明</span>${esc(row.stage_description || definition.description || '尚未填写描述。')}</p><p><span>最近变化</span>${esc(row.reason || '—')}</p>${scopeDetail}`;
  const categories = [{id:'',name:'未分类'},...data.categories];
  const siblings = data.definitions.filter(item => (item.category || '') === (definition.category || ''));
  const siblingIndex = siblings.findIndex(item => item.id === definition.id);
  return `<article class="state-entry ${definition.enabled?'':'disabled'} ${lifeRow?'life-owned':''} ${open?'is-open':''}" data-state-key="${esc(row.key)}">
    <button type="button" class="state-strip" data-action="state-open" data-key="${esc(row.key)}" aria-expanded="${open}" aria-controls="${detailId}">
      <span class="state-strip-copy"><span class="state-strip-name">${esc(definition.name)}${definition.enabled?'':' <span class="state-disabled-label">已停用</span>'}</span>${owner?`<span class="state-owner">${esc(owner)}</span>`:''}</span>
      <span class="state-strip-value">${esc(value)}${stage}</span>${meter}
    </button>
    <section id="${detailId}" class="state-detail" ${open?'':'hidden'} aria-label="${esc(definition.name)}详情">
      <div class="state-detail-copy">${info}<p class="state-detail-id">${esc(definition.id)} · ${esc(kindNames[definition.type] || definition.type)} · ${esc(scopeNames[scope] || scope)}</p></div>
      <div class="state-detail-actions">
        <button type="button" data-action="edit">${isGroup?'编辑指标':'编辑状态'}</button>
        ${isGroup?'':`<button type="button" data-action="adjust" ${canOperate?'':'disabled title="等待真实 QQ 会话首次使用"'}>调整值</button><button type="button" data-action="reset" ${canOperate?'':'disabled title="等待真实 QQ 会话首次使用"'}>重置</button>${definition.interactions?.length && !lifeRow?`<button type="button" data-action="interact" ${canOperate?'':'disabled title="等待真实 QQ 会话首次使用"'}>执行交互</button>`:''}`}
        <button type="button" data-action="toggle">${definition.enabled?'停用':'启用'}${isGroup?'指标':'状态'}</button>
        ${isGroup?'':`<button type="button" data-action="up" aria-label="上移 ${esc(definition.name)}" ${siblingIndex<=0?'disabled':''}>上移</button><button type="button" data-action="down" aria-label="下移 ${esc(definition.name)}" ${siblingIndex===siblings.length-1?'disabled':''}>下移</button>`}
        <label class="state-move-category">分类<select data-detail-category>${categories.map(category => `<option value="${esc(category.id)}" ${category.id===(definition.category || '')?'selected':''}>${esc(category.name)}</option>`).join('')}</select></label>
        <button type="button" data-action="move-category" ${categories.length<2?'disabled':''}>移动分类</button>
        <button type="button" class="danger" data-action="${isGroup?'delete-group':'delete'}">删除${isGroup?'指标':'状态'}</button>
      </div>
    </section>
  </article>`;
}
function fieldValue(name) { return $(`[name="${name}"]`, $('#stateForm')).value; }
function categoryOptions(value) { return [{id:'',name:'未分类'},...data.categories].map(c => `<option value="${esc(c.id)}" ${c.id === value?'selected':''}>${esc(c.name)}</option>`).join(''); }
function valueInput(kind, value, options = [], attr = '') {
  if (kind === 'boolean') return `<select ${attr}><option value="false" ${value === false?'selected':''}>否 / false</option><option value="true" ${value === true?'selected':''}>是 / true</option></select>`;
  if (kind === 'enum') return `<select ${attr}>${options.map(v => `<option value="${esc(v)}" ${v === value?'selected':''}>${esc(v)}</option>`).join('')}</select>`;
  return `<input ${attr} type="${kind === 'number'?'number':'text'}" ${kind === 'number'?'step="any"':''} value="${esc(value)}">`;
}
function renderEditor(state = blank(), isEditing = false) {
  draft = {...blank(),...structuredClone(state)}; editing = isEditing; builderMode='state';
  if (!editing && !draft.id) draft.id=makeId('state');
  if (!Array.isArray(draft.editor_blocks)) draft.editor_blocks=inferredBlocks(draft);
  else draft.editor_blocks=inferredBlocks(draft);
  const form = $('#stateForm'); form.reset();
  for (const field of ['id','name','description','type','scope','prompt','min','max']) form.elements[field].value = draft[field] ?? '';
  for (const field of ['enabled','inject','show_value']) form.elements[field].checked = draft[field] ?? true;
  form.elements.options.value = (draft.options || []).join('\n');
  form.elements.category.innerHTML = categoryOptions(draft.category);
  $('#initialField').innerHTML = valueInput(draft.type,draft.initial,draft.options,'name="initial" required');
  form.elements.rate_direction.value = Number(draft.rate_per_hour)<0?'decrease':'increase';
  form.elements.rate_amount.value = Math.abs(Number(draft.rate_per_hour) || 0);
  form.elements.id.readOnly=true;
  for (const field of ['type','scope']) form.elements[field].disabled = editing;
  $$('.numeric-only').forEach(n => n.hidden = draft.type !== 'number');
  $$('.enum-only').forEach(n => n.hidden = draft.type !== 'enum');
  const blockHost=$('#stateBuilderBlocks');
  for (const block of ['time','stages','interactions','events']) {
    const node=$(`#stateBuilderBlocks > details[data-block="${block}"]`),active=draft.editor_blocks.includes(block);
    const shouldOpen=active && (block==='stages'?draft.stages.length>0:block==='interactions'?draft.interactions.length>0:block==='events'?draft.events.length>0:false);
    node.hidden=false;
    node.dataset.removing=active?'false':'true';
    if (active) void setDetailsOpen(node,shouldOpen);
    else void setDetailsOpen(node,false).then(() => {if (node.dataset.removing==='true') node.hidden=true;});
  }
  reorderBlocks(blockHost,draft.editor_blocks);
  $('#editorTitle').textContent = editing ? `编辑状态 · ${draft.name}` : '新建状态';
  $('#editorHint').textContent = editing ? '修改定义不会重置运行值；状态类型、归属和技术标识保持不变。' : '组合状态积木，让运行中的状态按你设置的规则变化。';
  $('#cancelEditButton').hidden = false;
  $('#cancelEditButton').textContent = '取消';
  $('#builderKindSwitch').hidden=false;
  $('#baseKindButton').disabled=editing;$('#groupKindButton').disabled=editing;
  switchPanels([$('#stateForm'),$('#groupForm')],$('#stateForm'));
  $('#saveStateButton').textContent = editing ? '保存基础状态' : '创建基础状态';
  $('#formError').hidden = true;
  renderDynamic();
  void setDetailsOpen($('#advancedDetails'),false);
  $('#advancedDetails').hidden=false;
  showBuilderMode('state');
}
function targetDefinition(id) { return !id || id === 'self' || id === draft.id ? draft : data.definitions.find(s => s.id === id) || draft; }
function targetOptions(id) {
  if (id === draft.id) id = 'self';
  return [{id:'self',name:'当前状态'},...data.definitions.filter(s => s.id !== draft.id)].map(s => `<option value="${esc(s.id)}" ${(id || 'self') === s.id?'selected':''}>${esc(s.name)}</option>`).join('');
}
function operationOptions(op,kind,effect=false) {
  const values = effect ? (kind === 'number'?[['increase','增加'],['decrease','减少'],['set','设为']]:[['set','设为']]) : (kind === 'number'?[['gte','大于或等于'],['gt','大于'],['lte','小于或等于'],['lt','小于'],['eq','正好是'],['ne','不是']]:[['eq','正好是'],['ne','不是']]);
  return values.map(([key,label]) => `<option value="${key}" ${op === key?'selected':''}>${label}</option>`).join('');
}
function clauseHTML(c,effect,ruleField,ruleIndex,index) {
  const target = targetDefinition(c.state), list = effect?'effects':'conditions';
  const displayOp=effect && target.type==='number' && c.op==='add' ? Number(c.value)<0?'decrease':'increase' : c.op;
  const displayValue=effect && target.type==='number' && displayOp!=='set' ? Math.abs(Number(c.value) || 0) : c.value;
  const valueLabel=effect ? (displayOp==='set'?'设为':'变化数量') : '比较值';
  const clauses=document.querySelectorAll(`#${ruleField} .rule`)[ruleIndex]?.querySelectorAll(`.clause[data-list="${list}"]`).length || 0;
  return `<div class="clause ${effect?'effect-clause':''}" data-list="${list}" data-index="${index}"><label>${effect?'让':'状态'}<select data-field="state" class="clause-target">${targetOptions(c.state)}</select></label><label>${effect?'变化方式':'条件'}<select data-field="op">${operationOptions(displayOp,target.type,effect)}</select></label><label>${valueLabel}${valueInput(target.type,displayValue,target.options,'data-field="value"')}</label><div class="clause-controls"><button type="button" class="quiet small" data-action="clause-up" data-field="${ruleField}" data-rule="${ruleIndex}" data-list="${list}" data-index="${index}" aria-label="此项上移" ${index===0?'disabled':''}>↑</button><button type="button" class="quiet small" data-action="clause-down" data-field="${ruleField}" data-rule="${ruleIndex}" data-list="${list}" data-index="${index}" aria-label="此项下移" ${index===clauses-1?'disabled':''}>↓</button><button type="button" class="quiet small danger" data-action="remove-clause" data-field="${ruleField}" data-rule="${ruleIndex}" data-list="${list}" data-index="${index}" aria-label="移除此项">移除</button></div></div>`;
}
function ruleSummary() {
  const active=new Set(activeBuilderBlocks().map(node => node.dataset.block));
  const parts=[];
  if (active.has('time') && draft.type==='number') {
    const amount=Math.abs(Number($('#stateForm').elements.rate_amount.value) || 0),direction=$('#stateForm').elements.rate_direction.value==='decrease'?'减少':'增加';
    parts.push(`每小时${direction}${numberText(amount)}`);
  }
  if (active.has('stages')) for (const stage of draft.stages || []) if (stage.name) {
    const range=draft.type==='number'?`数值${stage.min??'不限'}至${stage.max??'不限'}`:`状态为${stage.value ?? ''}`;
    parts.push(`${range}时表现为“${stage.name}”${stage.description?`：${stage.description}`:''}`);
  }
  const clauseText=(clause,effect=false) => {
    const target=targetDefinition(clause.state),name=target.name || '当前状态',kind=target.type || draft.type;
    const raw=clause.value;
    if (effect && kind==='number') {
      if (clause.op==='set') return `让${name}设为${numberText(raw)}`;
      return `让${name}${Number(raw)<0?'减少':'增加'}${numberText(Math.abs(Number(raw)||0))}`;
    }
    const operators={gte:'大于或等于',gt:'大于',lte:'小于或等于',lt:'小于',eq:'正好是',ne:'不是'};
    return `${name}${operators[clause.op] || '等于'}${kind==='number'?numberText(raw):raw===true?'是':raw===false?'否':String(raw ?? '')}`;
  };
  if (active.has('interactions')) for (const interaction of draft.interactions || []) if (interaction.name) {
    const conditions=(interaction.conditions || []).map(clause => clauseText(clause)).join('，并且');
    const effects=(interaction.effects || []).map(clause => clauseText(clause,true)).join('；');
    parts.push(`交互“${interaction.name}”${conditions?`：当${conditions}时`:''}${effects?`，${effects}`:''}`);
  }
  if (active.has('events')) for (const event of draft.events || []) if (event.name) {
    const when=event.trigger==='threshold' && event.when ? `当${draft.name || '状态'}${clauseText({state:'self',op:event.when.op,value:event.when.value})}时` : `当对话符合“${event.description || event.name}”时`;
    const conditions=(event.conditions || []).map(clause => clauseText(clause)).join('，并且');
    const effects=(event.effects || []).map(clause => clauseText(clause,true)).join('；');
    parts.push(`事件“${event.name}”：${when}${conditions?`且${conditions}`:''}${effects?`，${effects}`:''}`);
  }
  const message=parts.length?`规则预览：${parts.join('；')}`:'添加积木后，会在这里显示规则摘要。';
  $('#stateRuleSummary').textContent=message;
}
function ruleHTML(r,field,index) {
  const event = field === 'events';
  return `<div class="rule" data-index="${index}"><div class="rule-head"><strong>${event?'事件':'交互'} ${index+1}</strong><div class="rule-controls"><button type="button" class="quiet small" data-action="rule-up" data-field="${field}" data-index="${index}" aria-label="${event?'事件':'交互'}上移" ${index===0?'disabled':''}>上移</button><button type="button" class="quiet small" data-action="rule-down" data-field="${field}" data-index="${index}" aria-label="${event?'事件':'交互'}下移" ${index===draft[field].length-1?'disabled':''}>下移</button><button type="button" class="quiet small danger" data-action="remove-rule" data-field="${field}" data-index="${index}">移除</button></div></div><div class="form-grid"><label>名称<input data-field="name" value="${esc(r.name)}" required></label><label class="wide">${event?'触发含义与判断示例':'用途与执行时机'}<textarea data-field="description" rows="2">${esc(r.description)}</textarea></label>${event?`<label>触发方式<select data-field="trigger" class="event-trigger"><option value="semantic" ${r.trigger !== 'threshold'?'selected':''}>对话含义 · 模型判断</option><option value="threshold" ${r.trigger === 'threshold'?'selected':''}>数值进入区间</option></select></label>`:`<div class="check-row wide"><label><input type="checkbox" data-field="allow_user" ${r.allow_user !== false?'checked':''}> 允许普通用户通过“状态交互”指令执行</label></div>`}</div>${event && r.trigger === 'threshold'?`<div class="threshold-fields"><label>当前状态进入<select data-field="when_op">${operationOptions(r.when?.op || 'gte',draft.type)}</select></label><label>阈值${valueInput(draft.type,r.when?.value ?? draft.initial,draft.options,'data-field="when_value"')}</label></div>`:''}<details class="rule-technical"><summary>高级 · 技术标识</summary><label>标识<input data-field="id" value="${esc(r.id || makeId(event?'event':'interaction'))}" readonly required></label></details><div class="rule-group"><div class="rule-group-head"><span>执行条件 · 全部满足${event?'，可不填写':''}</span><button type="button" class="quiet small" data-action="add-clause" data-field="${field}" data-rule="${index}" data-list="conditions">添加条件</button></div>${(r.conditions || []).map((c,i) => clauseHTML(c,false,field,index,i)).join('')}</div><div class="rule-group"><div class="rule-group-head"><span>状态变化</span><button type="button" class="quiet small" data-action="add-clause" data-field="${field}" data-rule="${index}" data-list="effects">添加变化</button></div>${(r.effects || []).map((c,i) => clauseHTML(c,true,field,index,i)).join('')}</div></div>`;
}
function renderDynamic() {
  $('#stages').innerHTML = draft.stages.map((s,i) => `<div class="stage-row" data-index="${i}"><div class="stage-fields"><label>阶段名称<input data-field="name" value="${esc(s.name)}" required></label>${draft.type === 'number'?`<label>包含下界<input data-field="min" type="number" step="any" value="${esc(s.min)}" placeholder="不限"></label><label>不含上界<input data-field="max" type="number" step="any" value="${esc(s.max)}" placeholder="不限"></label>`:`<label>匹配值${valueInput(draft.type,s.value,draft.options,'data-field="value"')}</label>`}<div class="stage-order"><button type="button" class="quiet small" data-action="stage-up" data-index="${i}" aria-label="阶段上移" ${i===0?'disabled':''}>上移</button><button type="button" class="quiet small" data-action="stage-down" data-index="${i}" aria-label="阶段下移" ${i===draft.stages.length-1?'disabled':''}>下移</button><button type="button" class="quiet small danger" data-action="remove-stage" data-index="${i}" aria-label="移除阶段">移除</button></div></div><label>该阶段的感受与表现<textarea data-field="description" rows="2">${esc(s.description)}</textarea></label></div>`).join('') || '<p class="help">添加阶段后，可描述不同数值下的感受与表现。</p>';
  for (const field of ['interactions','events']) {
    for (const rule of draft[field]) if (!rule.id) rule.id=makeId(field==='events'?'event':'interaction');
    $(`#${field}`).innerHTML = draft[field].map((r,i) => ruleHTML(r,field,i)).join('') || '<p class="help">添加后可配置名称、条件和状态变化。</p>';
  }
  ruleSummary();
}
function cast(value,kind,strict=true) {
  if (kind === 'boolean') return value === 'true';
  if (kind === 'number') {
    const result = value === ''?NaN:Number(value);
    if (!Number.isFinite(result) && strict) throw Error('请填写有效的数值');
    return Number.isFinite(result)?result:0;
  }
  return value;
}
function validateStateForm() {
  const form = $('#stateForm');
  const hiddenOutsideDetailsContent = element => {
    for (let node=element;node && node!==form;node=node.parentElement) {
      if (node.hidden && !node.classList.contains('details-content')) return true;
    }
    return false;
  };
  const invalid = [...form.elements].find(element => element.willValidate && !element.disabled && !hiddenOutsideDetailsContent(element) && !element.validity.valid);
  if (!invalid) return true;
  for (let details = invalid.closest('details'); details; details = details.parentElement?.closest('details')) void setDetailsOpen(details,true);
  invalid.scrollIntoView({behavior:'smooth',block:'center'});
  invalid.focus({preventScroll:true});
  invalid.reportValidity();
  return false;
}
function collect(strict=true) {
  const form = $('#stateForm');
  const result = {...draft};
  for (const key of ['id','name','description','category','scope','type','prompt']) result[key] = form.elements[key].value.trim();
  for (const key of ['enabled','inject','show_value']) result[key] = form.elements[key].checked;
  result.options = form.elements.options.value.split('\n').map(v => v.trim()).filter(Boolean);
  result.initial = cast(form.elements.initial.value,result.type,strict);
  for (const key of ['min','max']) result[key] = result.type === 'number' && form.elements[key].value !== ''?cast(form.elements[key].value,'number',strict):null;
  const activeBlocks=activeBuilderBlocks().map(node => node.dataset.block);
  result.editor_blocks=activeBlocks;
  const rateAmount=result.type==='number' && activeBlocks.includes('time')?Math.abs(cast(form.elements.rate_amount.value || '0','number',strict)):0;
  result.rate_per_hour = rateAmount * (form.elements.rate_direction.value==='decrease'?-1:1);
  draft = result;
  result.stages = activeBlocks.includes('stages') ? $$('#stages .stage-row').map(row => {
    const s = {name:$('[data-field="name"]',row).value,description:$('[data-field="description"]',row).value};
    if (result.type === 'number') for (const key of ['min','max']) { const value = $(`[data-field="${key}"]`,row).value; s[key] = value === ''?null:cast(value,'number',strict); }
    else s.value = cast($('[data-field="value"]',row).value,result.type,strict);
    return s;
  }) : [];
  for (const field of ['interactions','events']) result[field] = activeBlocks.includes(field) ? $$(`#${field} .rule`).map(row => {
    const r = {id:$('[data-field="id"]',row).value.trim(),name:$('[data-field="name"]',row).value.trim(),description:$('[data-field="description"]',row).value};
    for (const list of ['conditions','effects']) r[list] = $$(`.clause[data-list="${list}"]`,row).map(c => {
      const state = $('[data-field="state"]',c).value, target = targetDefinition(state);
      const choice=$('[data-field="op"]',c).value,value=cast($('[data-field="value"]',c).value,target.type,strict);
      if (list==='effects' && target.type==='number') return {state,op:choice==='set'?'set':'add',value:choice==='set'?value:choice==='decrease'?-Math.abs(value):Math.abs(value)};
      return {state,op:choice,value};
    });
    if (field === 'events') {
      r.trigger = $('[data-field="trigger"]',row).value;
      if ($('[data-field="when_op"]',row)) r.when = {op:$('[data-field="when_op"]',row).value,value:cast($('[data-field="when_value"]',row).value,result.type,strict)};
    } else r.allow_user = $('[data-field="allow_user"]',row).checked;
    return r;
  }) : [];
  return result;
}
function modal(title,html,onSubmit,button='保存') {
  $('#modalTitle').textContent = title; $('#modalBody').innerHTML = html; $('#modalError').hidden = true;
  $('#confirmModal').textContent = button; $('#cancelModal').textContent = '取消';
  $('#modalForm').onsubmit = async event => {
    event.preventDefault(); const save = $('#confirmModal'); save.disabled = true;
    try { await onSubmit($('#modalBody')); await closeDialog($('#modal')); }
    catch (error) { $('#modalError').textContent = error.message; $('#modalError').hidden = false; }
    finally { save.disabled = false; }
  };
  void showDialog($('#modal'));
}
function categoryDialog(category) {
  modal(category?'重命名分类':'新建分类',`<label>分类名称<input name="categoryName" required value="${esc(category?.name || '')}" autofocus></label>`,async body => {
    accept(await api('categories',{action:category?'rename':'create',id:category?.id || '',name:$('input',body).value})); toast('分类已保存');
  });
}
async function reorder(stateId,categoryId,targetId='',after=false) {
  const ids = data.definitions.map(s => s.id).filter(id => id !== stateId);
  let index = targetId?ids.indexOf(targetId):-1;
  if (index >= 0) index += after?1:0;
  else { const last = [...ids].reverse().find(id => data.definitions.find(s => s.id === id).category === categoryId); index = last?ids.indexOf(last)+1:ids.length; }
  ids.splice(index,0,stateId);
  accept(await api('order',{ids,categories:{[stateId]:categoryId}})); toast('排列已保存');
}
function defaultClause(effect=false) { return {state:'self',op:effect?(draft.type === 'number'?'add':'set'):(draft.type === 'number'?'gte':'eq'),value:draft.type === 'number'?0:draft.type === 'boolean'?false:draft.type === 'enum'?(draft.options[0] || ''):''}; }

document.addEventListener('click',event => {
  const addBlock=event.target.closest('[data-add-block]');
  if (addBlock) {
    if (builderMode!=='state') return;
    draft=collect(false);
    const block=addBlock.dataset.addBlock,node=$(`#stateBuilderBlocks > details[data-block="${block}"]`);
    node.hidden=false;node.dataset.removing='false';void setDetailsOpen(node,true);
    draft.editor_blocks=[...new Set([...draft.editor_blocks,block])];
    reorderBlocks($('#stateBuilderBlocks'),draft.editor_blocks);
    ruleSummary();
    return;
  }
  const button = event.target.closest('[data-action]'); if (!button) return;
  const action = button.dataset.action;
  (async () => {
    if (['remove-stage','stage-up','stage-down','remove-rule','rule-up','rule-down','add-clause','remove-clause','clause-up','clause-down','remove-block','block-up','block-down'].includes(action)) {
      draft = collect(false);
      const move=(items,index,step) => {const next=index+step;if(next<0||next>=items.length)return;[items[index],items[next]]=[items[next],items[index]];};
      if (action === 'remove-stage') draft.stages.splice(Number(button.dataset.index),1);
      if (action === 'stage-up' || action === 'stage-down') move(draft.stages,Number(button.dataset.index),action==='stage-up'?-1:1);
      if (action === 'remove-rule') draft[button.dataset.field].splice(Number(button.dataset.index),1);
      if (action === 'rule-up' || action === 'rule-down') move(draft[button.dataset.field],Number(button.dataset.index),action==='rule-up'?-1:1);
      if (action === 'add-clause') draft[button.dataset.field][Number(button.dataset.rule)][button.dataset.list].push(defaultClause(button.dataset.list === 'effects'));
      if (action === 'remove-clause') draft[button.dataset.field][Number(button.dataset.rule)][button.dataset.list].splice(Number(button.dataset.index),1);
      if (action === 'clause-up' || action === 'clause-down') move(draft[button.dataset.field][Number(button.dataset.rule)][button.dataset.list],Number(button.dataset.index),action==='clause-up'?-1:1);
      if (action === 'remove-block') {
        const block=button.dataset.block,node=$(`#stateBuilderBlocks > details[data-block="${block}"]`);
        node.dataset.removing='true';
        void setDetailsOpen(node,false).then(() => {if (node.dataset.removing==='true') node.hidden=true;});
        draft.editor_blocks=draft.editor_blocks.filter(name => name!==block);
        if (block==='time') {draft.rate_per_hour=0;$('#stateForm').elements.rate_direction.value='increase';$('#stateForm').elements.rate_amount.value='0';}
        if (block==='stages') draft.stages=[];
        if (block==='interactions' || block==='events') draft[block]=[];
      }
      if (action === 'block-up' || action === 'block-down') {
        const blocks=activeBuilderBlocks().map(node => node.dataset.block),index=blocks.indexOf(button.dataset.block),step=action==='block-up'?-1:1;
        move(blocks,index,step);reorderBlocks($('#stateBuilderBlocks'),blocks);draft.editor_blocks=blocks;
      }
      draft.editor_blocks=activeBuilderBlocks().map(node => node.dataset.block);
      renderDynamic(); return;
    }
    if (action === 'state-open') {
      const rowKey=button.dataset.key;
      selectedStateKey = selectedStateKey === rowKey ? '' : rowKey;
      renderStates();
      $$('.state-strip').find(node => node.dataset.key === rowKey)?.focus({preventScroll:true});
      return;
    }
    if (action === 'new') { editing=false;groupEditor?.reset();renderEditor();tab('create');return; }
    if (action === 'filter') { categoryFilter = button.dataset.category; renderStates(); return; }
    if (action.startsWith('category-')) {
      const category = data.categories.find(c => c.id === button.dataset.category);
      if (!category) return;
      if (action === 'category-rename') categoryDialog(category);
      else modal('删除分类',`<p>删除“${esc(category.name)}”后，其中的基础状态和综合指标将移至“未分类”，运行值会保留。</p>`,async () => {accept(await api('categories',{action:'delete',id:category.id}));toast('分类已删除');},'删除分类');
      return;
    }
    const entry = button.closest('[data-state-key]');
    const runtime = entry && data.states.find(row => row.key === entry.dataset.stateKey);
    const state = runtime && (data.definitions.find(definition => definition.id === runtime.id) || data.groups.find(definition => definition.id === runtime.id));
    if (!state || !runtime) return;
    const isGroup=state.kind==='group' || runtime.kind==='group' || runtime.computed===true;
    const operationContext = () => {
      if (isLifeStateRow(runtime)) {
        if (state.scope!=='character' || !runtime.account_id) throw Error('只有角色整体生活状态可以在没有聊天会话时调整。');
        return {account_id:runtime.account_id};
      }
      if (runtime.unbound || !runtime.context) throw Error('等待真实 QQ 会话首次使用后，才能调整或重置运行值。');
      const viewContext = indoorView?.getActor?.();
      return {context:runtime.context,...(viewContext?{view_context:viewContext}:{})};
    };
    if (action === 'edit') {
      if (isGroup) {
        editing=true;showBuilderMode('group');groupEditor.editGroup(state);
        $('#editorTitle').textContent=`编辑综合指标 · ${state.name}`;$('#editorHint').textContent='修改指标不会重置来源状态；技术标识保持不变。';
        $('#builderKindSwitch').hidden=false;$('#baseKindButton').disabled=true;$('#groupKindButton').disabled=true;
      } else renderEditor(state,true);
      tab('create');window.scrollTo({top:0,behavior:'smooth'});
    }
    if (action === 'toggle') await run(button,async () => {
      if (isGroup) {accept(await api('groups/save',{group:{...state,enabled:!state.enabled},create:false}));toast(state.enabled?'综合指标已停用':'综合指标已启用');}
      else {accept(await api('states/save',{definition:{...state,enabled:!state.enabled},create:false}));toast(state.enabled?'状态已停用':'状态已启用');}
    });
    if (action === 'adjust' && !runtime.unbound) modal('调整当前值',`<p>只修改“${esc(state.name)}”在“${esc(state.scope==='character'?'角色整体':ownerLabel(state,runtime))}”中的当前值，不修改初始值。</p><label>当前值${valueInput(state.type,runtime.value,state.options,'name="runtimeValue" required')}</label>`,async body => {accept(await api('runtime',{id:state.id,value:cast($('[name="runtimeValue"]',body).value,state.type),...operationContext()}));toast('当前值已更新');});
    if (action === 'reset' && !runtime.unbound) modal('重置当前值',`<p>将“${esc(state.name)}”在“${esc(state.scope==='character'?'角色整体':ownerLabel(state,runtime))}”中的当前值重置为初始值 ${esc(rawStateValue(state,state.initial))}。</p>`,async () => {accept(await api('runtime',{id:state.id,reset:true,...operationContext()}));toast('当前值已重置');},'重置');
    if (action === 'delete') {
      const instanceCount=data.states.filter(row => row.id===state.id).length;
      modal('删除状态',`<p>删除“${esc(state.name)}”的定义和所有归属数据，包含 ${instanceCount} 条已显示的运行记录。其他状态仍在引用它时，需要先移除相关规则。</p>`,async () => {accept(await api('states/delete',{id:state.id}));toast('状态已删除');},'删除状态');
    }
    if (action === 'delete-group') modal('删除综合指标',`<p>删除“${esc(state.name)}”的定义。来源基础状态和它们的运行值会保留；仍被其他规则引用时，系统会提示先解除引用。</p>`,async () => {accept(await api('groups/delete',{id:state.id}));toast('综合指标已删除');},'删除指标');
    if (action === 'interact' && !runtime.unbound) modal('执行交互',`<p>交互只针对“${esc(state.name)}”的“${esc(state.scope==='character'?'角色整体':ownerLabel(state,runtime))}”归属，程序会检查条件并更新相关状态。</p><label>选择交互<select name="interaction">${(state.interactions || []).map(r => `<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.description)}</option>`).join('')}</select></label>`,async body => {accept(await api('interact',{state_id:state.id,interaction_id:$('select',body).value,token:crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,...operationContext()}));toast('交互已执行');},'执行');
    if (action === 'up' || action === 'down') {
      const siblings = data.definitions.filter(s => (s.category || '') === (state.category || '')), index = siblings.findIndex(s => s.id === state.id), target = siblings[index + (action === 'up'?-1:1)];
      if (target) await run(button,() => reorder(state.id,state.category || '',target.id,action === 'down'));
    }
    if (action === 'move-category') {
      const category=$('[data-detail-category]',entry)?.value;
      if (category === (state.category || '')) return;
      await run(button,async () => {
        const ids=data.definitions.map(definition => definition.id);
        const result=isGroup
          ? await api('groups/save',{group:{...state,category},create:false})
          : await api('order',{ids,categories:{[state.id]:category}});
        categoryFilter=category;accept(result);toast('分类已更新');
      });
    }
  })().catch(() => {});
});

for (const button of $$('[data-main-tab]')) button.onclick = () => mainTab(button.dataset.mainTab);
const initiallySelectedMainTab=$('[data-main-tab][aria-selected="true"]');
if (initiallySelectedMainTab) revealMainTab(initiallySelectedMainTab);
$('.primary-tabs').onkeydown = event => {
  if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
  event.preventDefault();
  const buttons = $$('[data-main-tab]');
  const index = buttons.findIndex(button => button.getAttribute('aria-selected') === 'true');
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
  mainTab(buttons[next].dataset.mainTab);buttons[next].focus();
};
$('#viewTab').onclick = () => tab('view');$('#createTab').onclick = () => tab('create');
$('#stateTabs').onkeydown = event => {
  if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
  event.preventDefault();
  const tabs = [$('#viewTab'),$('#createTab')];
  const selected = tabs.findIndex(button => button.getAttribute('aria-selected') === 'true');
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (selected + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
  tab(next === 0 ? 'view' : 'create',true);
};
$('#addCategoryButton').onclick = () => categoryDialog();
$('#baseKindButton').onclick = () => switchBuilderMode('state');
$('#groupKindButton').onclick = () => switchBuilderMode('group');
$('#cancelEditButton').onclick = () => {editing=false;groupEditor?.reset();renderEditor(blank());tab('view',true);};
$('#closeModal').onclick = $('#cancelModal').onclick = () => {void closeDialog($('#modal'));};
$('#addStageButton').onclick = () => {activateStateBlock('stages');draft=collect(false);draft.stages.push({name:'',description:'',min:null,max:null,value:draft.initial});renderDynamic();};
for (const [button,field] of [['#addInteractionButton','interactions'],['#addEventButton','events']]) $(button).onclick = () => {activateStateBlock(field);draft=collect(false);draft[field].push({id:makeId(field==='events'?'event':'interaction'),name:'',description:'',allow_user:true,trigger:'semantic',conditions:[],effects:[defaultClause(true)]});renderDynamic();};
$('#stateForm').elements.type.onchange = () => {
  draft=collect(false);draft.type=$('#stateForm').elements.type.value;draft.initial=draft.type === 'number'?0:draft.type === 'boolean'?false:draft.type === 'enum'?(draft.options[0] || ''):'';draft.stages=[];
  if (draft.type!=='number') {draft.rate_per_hour=0;draft.editor_blocks=draft.editor_blocks.filter(block => block!=='time');}
  renderEditor(draft,false);
};
$('#stateForm').elements.options.onchange = () => {
  const value = $('#stateForm').elements.initial.value, options = fieldValue('options').split('\n').map(s => s.trim()).filter(Boolean);
  $('#initialField').innerHTML = valueInput('enum',options.includes(value)?value:(options[0] || ''),options,'name="initial" required');
  draft=collect(false);renderDynamic();
};
$('#stateForm').addEventListener('change',event => {
  if (event.target.matches('.clause-target,.event-trigger,.clause [data-field="op"]')) {
    const field=event.target.closest('.rule')?.parentElement?.id,ruleIndex=Number(event.target.closest('.rule')?.dataset.index),fieldName=event.target.dataset.field;
    draft=collect(false);renderDynamic();
    const replacement=$(`#${field} .rule[data-index="${ruleIndex}"] [data-field="${fieldName}"]`);replacement?.focus({preventScroll:true});
  } else if (!event.target.matches('[name="type"],[name="options"]')) {
    draft=collect(false);ruleSummary();
  }
});
$('#stateForm').addEventListener('input',event => {if (!event.target.matches('.clause-target,.event-trigger')) draft=collect(false);ruleSummary();});
$('#stateForm').onsubmit = async event => {
  event.preventDefault();const button=$('#saveStateButton');button.disabled=true;$('#formError').hidden=true;
  try {if (!validateStateForm()) return;const definition=collect();accept(await api('states/save',{definition,create:!editing}));editing=false;renderEditor(blank());categoryFilter='*';tab('view');toast('基础状态已保存');}
  catch(error) {$('#formError').textContent=error.message;$('#formError').hidden=false;}
  finally {button.disabled=false;}
};

scheduleView = mountSchedule({api,accept,run,toast,modal,getData:() => data,refresh,escape:esc});
indoorView = mountIndoor({api,accept,run,toast,modal,getData:() => data,getActor:() => actor,onContextChange:async selection => {
  const previous=selectedLifeAccountId;
  selectedLifeAccountId=String(selection?.account_id || '');
  try {await refresh(selection?.account_id ? {account_id:selectedLifeAccountId} : selection?.context || null);}
  catch (error) {selectedLifeAccountId=previous;throw error;}
},refresh,escape:esc});
lifeView = mountLife({api,run,toast,getData:() => data,escape:esc,onChange:life => {data.life=life;indoorView?.accept(data);refresh().catch(() => {});}});
sharingView = mountSharing({api,getData:() => data,toast});
groupEditor = mountGroupEditor({api,accept,run,toast,getData:() => data,escape:esc,onSaved:() => {editing=false;groupEditor.reset();renderEditor(draft,false);categoryFilter='*';tab('view');}});
groupEditor.reset();
$('#retrySyncButton').onclick=async event => {
  const button=event.currentTarget;button.disabled=true;setSyncStatus('正在重试同步…');
  try {await refresh();}
  catch(error) {setSyncStatus(`同步失败 · ${error.message || '连接暂不可用'}`,true);}
  finally {button.disabled=false;}
};
document.addEventListener('visibilitychange',() => {
  if (document.visibilityState==='visible') startPolling(true);
  else {clearInterval(pollTimer);pollTimer=0;}
});
window.addEventListener('pageshow',() => {if (document.visibilityState==='visible') startPolling(true);});

try {
  bridge=window.AstrBotPluginView;
  if (!bridge) throw Error('请从 AstrBot 插件详情页打开此 View。');
  const context = await bridge.ready();
  const applyTheme = value => { document.documentElement.dataset.theme = value?.isDark ? 'dark' : 'light'; };
  applyTheme(context);bridge.onContext(applyTheme);
  await refresh();await sharingView.load();renderEditor();startPolling();
} catch(error) {
  setSyncStatus(`同步失败 · ${error.message || '连接暂不可用'}`,true);
  $('#connectionError').textContent=error.message;$('#connectionError').hidden=false;$('#stateGroups').innerHTML='<div class="empty">状态未能加载；连接恢复后会自动重试。</div>';scheduleView?.loadFailed(error);indoorView?.loadFailed(error);startPolling();
}
