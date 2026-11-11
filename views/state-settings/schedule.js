import {switchPanels} from './motion.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const DAY_PRESETS = {daily:[0,1,2,3,4,5,6],weekdays:[0,1,2,3,4],weekends:[5,6]};
const COMMON_TIMEZONES = ['Asia/Shanghai','Asia/Taipei','Asia/Hong_Kong','Asia/Tokyo','Asia/Singapore','Asia/Seoul','UTC','Australia/Sydney','Europe/London','Europe/Paris','America/Los_Angeles','America/New_York'];

export function mountSchedule({api,accept,run,toast,modal,getData,refresh,escape}) {
  const panel = $('#schedulePanel');
  const form = $('#scheduleForm');
  const timezoneForm = $('#timezoneForm');
  const timezoneInput = $('#scheduleTimezone');
  const scheduleTabs = [$('#scheduleViewTab'),$('#scheduleCreateTab')];
  const schedulePanels = [$('#scheduleViewPanel'),$('#scheduleCreatePanel')];
  let editingId = null;
  let timezoneDirty = false;
  let scheduleWriteBusy = false;
  let categoryFilter = '*';

  function scheduleData() {
    const schedule=getData().schedule || {timezone:'Asia/Shanghai',entries:[]};
    return {...schedule,categories:Array.isArray(schedule.categories)?schedule.categories:[],entries:Array.isArray(schedule.entries)?schedule.entries:[]};
  }
  function scheduleRuntime() {
    return getData().schedule_runtime || {local_time:null,active:null,next:null};
  }
  function selectScheduleTab(which,focus = false) {
    const index = which === 'create' ? 1 : 0;
    scheduleTabs.forEach((tab,i) => {tab.setAttribute('aria-selected',String(i === index));tab.tabIndex = i === index ? 0 : -1;});
    switchPanels(schedulePanels,schedulePanels[index]);
    if (focus) scheduleTabs[index].focus();
  }
  function numericStates() {
    return (getData().definitions || []).filter(state => state.type === 'number');
  }
  function setError(selector,message) {
    const node = $(selector);
    node.textContent = message;
    node.hidden = !message;
  }
  function timezoneFormatOptions(options = {}) {
    return {timeZone:scheduleData().timezone || 'Asia/Shanghai',...options};
  }
  function dateLabel(value, options = {}) {
    if (!value) return '—';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '—';
    try { return new Intl.DateTimeFormat('zh-CN',timezoneFormatOptions(options)).format(date); }
    catch { return '—'; }
  }
  function timeLabel(value) {
    return dateLabel(value,{hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
  }
  function fullDateTime(value) {
    return dateLabel(value,{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
  }
  function renderSummary() {
    const runtime = scheduleRuntime();
    const active = runtime.active;
    const next = runtime.next;
    setText('#activeScheduleName',active?.name || '当前没有正在进行的固定日程');
    setText('#activeScheduleTime',active
      ? `${timeLabel(active.start_at)}–${timeLabel(active.end_at)} · ${scheduleData().timezone}`
      : runtime.local_time ? `当前时间 ${fullDateTime(runtime.local_time)} · ${scheduleData().timezone}` : `日程时区：${scheduleData().timezone}`);
    setText('#activeScheduleEnd',active?.end_at ? fullDateTime(active.end_at) : '—');
    setText('#activeScheduleEndDetail',active?.end_at ? `按 ${scheduleData().timezone} 计算` : '没有固定日程需要结束');
    setText('#nextScheduleName',next?.name || '暂无后续固定日程');
    setText('#nextScheduleTime',next?.start_at ? `${fullDateTime(next.start_at)} · ${scheduleData().timezone}` : '');
  }
  function setText(selector,value) {const node=$(selector),text=String(value ?? '');if(node.textContent!==text)node.textContent=text;}
  function categoryName(value) {return String(value || '').trim() || '未分类';}
  function visibleCategories() {return [...new Set([...scheduleData().categories.map(String),...lifeScheduleRows().map(item=>categoryName(item.category))])];}
  function categoryOptions(selected='') {
    const categories=scheduleData().categories.map(String),exists=categories.includes(String(selected || ''));
    return `<option value="">未分类</option>${selected && !exists?`<option value="${escape(selected)}" selected>${escape(selected)}（已保存的分类）</option>`:''}${categories.map(name=>`<option value="${escape(name)}" ${name===selected?'selected':''}>${escape(name)}</option>`).join('')}`;
  }
  function renderCategoryControls() {
    const categories=visibleCategories(),filter=$('#scheduleCategoryFilters'),actions=$('#scheduleCategoryActions');
    if(categoryFilter!=='*' && !categories.includes(categoryFilter))categoryFilter='*';
    const html=[['*','全部'],...categories.map(name=>[name,name])].map(([value,label])=>`<button type="button" class="${categoryFilter===value?'active':''}" data-schedule-category-filter="${escape(value)}">${escape(label)}</button>`).join('');
    if(filter.dataset.renderMarkup!==html){filter.innerHTML=html;filter.dataset.renderMarkup=html;}
    const selected=categoryFilter!=='*' && categoryFilter!=='未分类' && categories.includes(categoryFilter)?categoryFilter:'';
    actions.innerHTML=selected?`<button type="button" class="quiet small" data-schedule-category-action="rename" data-category="${escape(selected)}">重命名</button><button type="button" class="quiet small danger" data-schedule-category-action="delete" data-category="${escape(selected)}">删除分类</button>`:'';
  }
  function lifeScheduleRows() {
    const life=getData().life || {},presets=life.presets || [],rows=[];
    for(const preset of presets){
      const enabledAccounts=(preset.enabled_accounts || []).length,source=Array.isArray(preset.contents?.schedule)?preset.contents.schedule:[];
      const applies=(action,id)=>{
        const ids=Array.isArray(action.preset_ids)?action.preset_ids.map(String):action.preset_id?[String(action.preset_id)]:[];
        return ids.length?ids.includes(String(id)):true;
      };
      const actions=(life.actions || []).filter(action=>applies(action,preset.id));
      const expanded=source.flatMap(item=>Array.isArray(item.windows)?item.windows.map(window=>({...item,...window})): [item]);
      const windows=expanded.length?expanded:actions.flatMap(action=>(action.windows || []).map(window=>({...window,name:action.name,description:action.description,category:action.category,action_id:action.id,enabled:action.enabled!==false,preset_ids:action.preset_ids})));
      for(const [index,item] of windows.entries()){
        if(!applies(item,preset.id))continue;
        rows.push({...item,id:String(item.id || `${preset.id || 'life'}-${index}`),category:String(item.category || ''),enabled:item.enabled!==false && preset.installed!==false && enabledAccounts>0,paused:item.enabled===false || preset.installed===false || enabledAccounts===0,accounts:enabledAccounts});
      }
    }
    return rows;
  }
  function parseNode(markup) {const template=document.createElement('template');template.innerHTML=markup.trim();const node=template.content.firstElementChild;node.dataset.renderMarkup=markup;return node;}
  function placeChildren(parent,nodes) {
    let cursor=parent.firstElementChild;
    for(const node of nodes){if(node===cursor)cursor=cursor.nextElementSibling;else parent.insertBefore(node,cursor);}
    const keep=new Set(nodes);for(const child of [...parent.children])if(!keep.has(child))child.remove();
  }
  function dayLabel(days) {
    const normalized = [...new Set((days || []).map(Number).filter(day => Number.isInteger(day) && day >= 0 && day <= 6))].sort((a,b) => a-b);
    if (normalized.length === 7) return '每天';
    if (normalized.length === 5 && normalized.every((day,index) => day === index)) return '工作日';
    if (normalized.length === 2 && normalized[0] === 5 && normalized[1] === 6) return '周末';
    return normalized.length ? normalized.map(day => WEEKDAYS[day]).join('、') : '未选择星期';
  }
  function signedRate(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    if (number === 0) return '0';
    return `${number > 0 ? '+' : '−'}${Math.abs(number)}`;
  }
  function rateDescription(rates) {
    const statesById = new Map(numericStates().map(state => [state.id,state]));
    const rows = Object.entries(rates || {});
    if (!rows.length) return '<span>沿用基础变化速率</span>';
    return rows.map(([id,value]) => {
      const state = statesById.get(id);
      const name = state ? state.name : `未知状态 ${id}`;
      return `<span>${escape(name)} ${escape(signedRate(value))} / 小时${state?'':'（待移除）'}</span>`;
    }).join('');
  }
  function timeDescription(entry) {
    if (entry.start === entry.end) return `${entry.start} · 全天 24 小时`;
    if (entry.end < entry.start) return `${entry.start}–${entry.end} · 次日结束`;
    return `${entry.start}–${entry.end}`;
  }
  function renderList() {
    const {entries = []} = scheduleData();
    const list = $('#scheduleList');
    const manual=[...entries].sort((a,b) => String(a.start).localeCompare(String(b.start)) || String(a.name).localeCompare(String(b.name),'zh-CN'));
    const presetRows=lifeScheduleRows(),visibleManual=manual.filter(entry=>categoryFilter==='*' || categoryName(entry.category)===categoryFilter),visiblePresets=presetRows.filter(entry=>categoryFilter==='*' || categoryName(entry.category)===categoryFilter);
    $('#scheduleCount').textContent = `${manual.length + presetRows.length} 条`;
    if (!visibleManual.length && !visiblePresets.length) {
      const html=manual.length || presetRows.length?'<div class="empty"><p>此分类下没有安排。</p></div>':'<div class="empty"><p>还没有安排日程</p><button type="button" class="primary" data-schedule-action="new">新建第一条日程</button></div>';
      if(list.dataset.renderMarkup!==html){list.innerHTML=html;list.dataset.renderMarkup=html;}
      return;
    }
    const categoryGroups=[...new Set([...visibleManual.map(entry=>categoryName(entry.category)),...visiblePresets.map(entry=>categoryName(entry.category))])];
    const sections=categoryGroups.map(name=>{
      const category=name==='未分类'?'':name,manualRows=visibleManual.filter(entry=>categoryName(entry.category)===name),lifeRows=visiblePresets.filter(entry=>categoryName(entry.category)===name);
      const nodes=manualRows.map(entry => {
      const isEnabled = entry.enabled !== false;
      const html=`<article class="schedule-row" role="listitem" data-schedule="${escape(entry.id)}">
        <div class="schedule-row-time"><strong>${escape(timeDescription(entry))}</strong></div>
        <div class="schedule-row-identity"><strong>${escape(entry.name)}</strong><p>${escape(entry.description || '未填写描述')}</p><span class="schedule-category-label">${escape(name)}</span></div>
        <div class="schedule-row-days">${escape(dayLabel(entry.days))}</div>
        <div class="schedule-row-rates">${rateDescription(entry.rates)}</div>
        <div class="schedule-row-actions"><span class="schedule-status ${isEnabled?'is-enabled':''}">${isEnabled?'已启用':'已停用'}</span><div><button type="button" class="quiet" data-schedule-action="edit" aria-label="编辑 ${escape(entry.name)}">编辑</button><button type="button" class="quiet" data-schedule-action="toggle">${isEnabled?'停用':'启用'}</button><button type="button" class="quiet danger" data-schedule-action="delete" aria-label="删除 ${escape(entry.name)}">删除</button></div></div>
      </article>`;
      return html;
      });
      const presetHtml=lifeRows.map(item=>`<article class="schedule-row life-schedule-row" role="listitem" data-life-schedule="${escape(item.id)}"><div class="schedule-row-time"><strong>${escape(item.start || '—')}–${escape(item.end || '—')}</strong><small>${escape(dayLabel(item.days || []))}</small></div><div class="schedule-row-identity"><strong>${escape(item.name || '生活安排')}</strong><p>${escape(item.description || '可执行时段')}</p><span class="schedule-category-label">${escape(name)}</span></div><div class="schedule-row-days">可执行时段</div><div class="schedule-row-rates">${item.accounts?`已启用 ${item.accounts} 个 QQ`:'等待启用'}</div><div class="schedule-row-actions"><span class="schedule-status ${item.paused?'':'is-enabled'}">${item.paused?'已暂停':'可执行'}</span></div></article>`);
      return `<section class="schedule-category-section"><h4>${escape(name)}</h4>${nodes.join('')}${presetHtml.join('')}</section>`;
    }).join('');
    if(list.dataset.renderMarkup!==sections){list.innerHTML=sections;list.dataset.renderMarkup=sections;}
  }
  function daysPreset(days) {
    const normalized = [...new Set((days || []).map(Number))].sort((a,b) => a-b);
    for (const [preset,presetDays] of Object.entries(DAY_PRESETS)) {
      if (normalized.length === presetDays.length && normalized.every((day,index) => day === presetDays[index])) return preset;
    }
    return 'custom';
  }
  function currentRateRows() {
    return $$('.schedule-rate-row', $('#scheduleRateRows')).map(row => ({
      state:$('.rate-state',row)?.value || '',
      value:$('.rate-value',row)?.value ?? ''
    }));
  }
  function rateStateOptions(selected) {
    const states = numericStates();
    const exists = states.some(state => state.id === selected);
    const missing = selected && !exists ? `<option value="${escape(selected)}" selected>未知／非数值状态（${escape(selected)}）</option>` : '';
    return `${missing}${states.map(state => `<option value="${escape(state.id)}" ${state.id === selected?'selected':''}>${escape(state.name)} · ${escape(state.id)}</option>`).join('')}`;
  }
  function renderRateRows(rows = []) {
    const host = $('#scheduleRateRows');
    if (!rows.length) {
      host.innerHTML = '<p class="help">未添加覆盖。</p>';
      return;
    }
    host.innerHTML = rows.map((row,index) => `<div class="schedule-rate-row" data-index="${index}">
      <label>数值状态<select class="rate-state" required>${rateStateOptions(String(row.state || ''))}</select></label>
      <label>每小时覆盖量<input class="rate-value" type="number" step="any" value="${escape(row.value ?? '')}" required></label>
      <button type="button" class="quiet danger remove-rate" data-schedule-action="remove-rate" aria-label="移除第 ${index+1} 条数值覆盖">移除</button>
    </div>`).join('');
  }
  function renderEditor(entry = null,activate = true) {
    editingId = entry?.id || '';
    form.reset();
    form.elements.name.value = entry?.name || '';
    form.elements.category.innerHTML=categoryOptions(String(entry?.category || ''));
    form.elements.category.value=String(entry?.category || '');
    form.elements.description.value = entry?.description || '';
    form.elements.start.value = entry?.start || '';
    form.elements.end.value = entry?.end || '';
    form.elements.enabled.checked = entry ? entry.enabled !== false : true;
    const days = entry?.days || DAY_PRESETS.daily;
    form.elements.dayPreset.value = daysPreset(days);
    $$('#customDays input[name="days"]').forEach(input => { input.checked = days.map(Number).includes(Number(input.value)); });
    $('#customDays').hidden = form.elements.dayPreset.value !== 'custom';
    $('#scheduleEditorTitle').textContent = entry ? `编辑日程 · ${entry.name}` : '新建日程';
    $('#scheduleEditorHint').textContent = entry ? '修改后保存才会更新日程配置。' : '设置活动时间、重复日期和可选的数值覆盖。';
    setError('#scheduleFormError','');
    const rows = Object.entries(entry?.rates || {}).map(([state,value]) => ({state,value}));
    renderRateRows(rows);
    updateTimeHint();
    if (activate) {
      selectScheduleTab('create');
      $('#scheduleCreatePanel').scrollIntoView({behavior:'smooth',block:'nearest'});
      form.elements.name.focus({preventScroll:true});
    }
  }
  function clearEditor() {
    editingId = null;
    form.reset();
    form.elements.category.innerHTML=categoryOptions('');
    $('#customDays').hidden = true;
    setError('#scheduleFormError','');
    renderRateRows([]);
    updateTimeHint();
  }
  function startNewSchedule(focusTab = false) {
    if (editingId === null) renderEditor();
    else selectScheduleTab('create');
    if (focusTab) scheduleTabs[1].focus();
  }
  function updateTimeHint() {
    const start = form.elements.start.value;
    const end = form.elements.end.value;
    const timezone = scheduleData().timezone || 'Asia/Shanghai';
    let message = `时区 ${timezone} · 相同时间表示全天 24 小时；结束早于开始则跨至次日，重复星期按开始当天计算。`;
    if (start && end && start === end) message = `时区 ${timezone} · 开始和结束相同，按全天 24 小时处理；重复星期按当天计算。`;
    else if (start && end && end < start) message = `时区 ${timezone} · 结束早于开始，将在次日结束；重复星期按开始当天计算。`;
    else if (start && end) message = `时区 ${timezone} · 这条日程在同一天结束；重复星期按当天计算。`;
    $('#scheduleTimeHint').textContent = message;
  }
  function collectEntry() {
    const preset = form.elements.dayPreset.value;
    const days = preset === 'custom'
      ? $$('#customDays input[name="days"]:checked').map(input => Number(input.value)).sort((a,b) => a-b)
      : [...(DAY_PRESETS[preset] || [])];
    if (!days.length) throw Error('请至少选择一个重复星期。');
    const rates = {};
    for (const row of $$('.schedule-rate-row', $('#scheduleRateRows'))) {
      const state = $('.rate-state',row).value;
      const valueText = $('.rate-value',row).value;
      const definition = numericStates().find(item => item.id === state);
      if (!definition) throw Error('数值覆盖只能选择仍存在的数值状态；请更换或移除此项。');
      if (!valueText.trim() || !Number.isFinite(Number(valueText))) throw Error(`“${definition.name}”的每小时覆盖量必须是有效数值。`);
      if (Object.hasOwn(rates,state)) throw Error(`“${definition.name}”不能重复设置覆盖量。`);
      rates[state] = Number(valueText);
    }
    const name = form.elements.name.value.trim();
    const start = form.elements.start.value;
    const end = form.elements.end.value;
    if (!name) throw Error('请填写日程名称。');
    if (!/^\d{2}:\d{2}$/.test(start) || !/^\d{2}:\d{2}$/.test(end)) throw Error('请填写有效的开始和结束时间。');
    return {
      id:editingId || `schedule_${globalThis.crypto?.randomUUID?.().replaceAll('-','').toLowerCase() || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`}`,
      name,
      description:form.elements.description.value.trim(),
      category:form.elements.category.value,
      enabled:form.elements.enabled.checked,
      days,
      start,
      end,
      rates
    };
  }
  function render() {
    const schedule = scheduleData();
    $('#scheduleLoadError').hidden = true;
    renderSummary();
    renderCategoryControls();
    renderList();
    if (!timezoneDirty && document.activeElement !== timezoneInput) timezoneInput.value = schedule.timezone || 'Asia/Shanghai';
    $('#timezoneCurrent').textContent = `所有日程按 ${schedule.timezone || 'Asia/Shanghai'} 解释`;
    updateTimeHint();
  }
  async function withWriteGate(button,action) {
    if (scheduleWriteBusy) {
      toast('已有日程配置正在保存，请稍后重试。',true);
      return false;
    }
    scheduleWriteBusy = true;
    try { return await run(button,action); }
    finally { scheduleWriteBusy = false; }
  }
  function loadFailed(error) {
    const node = $('#scheduleLoadError');
    $('span',node).textContent = `日程未能加载：${error?.message || '请检查连接后重试。'}`;
    node.hidden = false;
  }
  function reload(button) {
    return run(button,async () => {
      $('#scheduleLoadError').hidden = true;
      try { await refresh(); }
      catch (error) { loadFailed(error); throw error; }
    }).catch(() => {});
  }
  function saveTimezone(event) {
    event.preventDefault();
    const button = $('#saveTimezoneButton');
    const timezone = timezoneInput.value.trim();
    setError('#timezoneError','');
    try { new Intl.DateTimeFormat('en-US',{timeZone:timezone}).format(new Date()); }
    catch { setError('#timezoneError','请输入有效的 IANA 时区名称，例如 Asia/Shanghai。'); return; }
    withWriteGate(button,async () => {
      const current = scheduleData();
      try {
        const result = await api('schedule/save',{schedule:{timezone,categories:current.categories,entries:current.entries}});
        timezoneDirty = false;
        accept(result);
        timezoneInput.value = scheduleData().timezone || timezone;
        setError('#timezoneError','');
        toast('日程时区已保存');
      } catch (error) {
        setError('#timezoneError',error.message || '日程时区保存失败，请重试。');
      }
    }).catch(() => {});
  }
  function saveEntry(event) {
    event.preventDefault();
    const button = $('#saveScheduleButton');
    setError('#scheduleFormError','');
    let entry;
    try {
      if (!form.reportValidity()) return;
      entry = collectEntry();
    } catch (error) { setError('#scheduleFormError',error.message); return; }
    const current = scheduleData();
    const entries = [...(current.entries || [])];
    const index = entries.findIndex(item => item.id === entry.id);
    if (index < 0) entries.push(entry); else entries[index] = entry;
    withWriteGate(button,async () => {
      try {
        const result = await api('schedule/save',{schedule:{timezone:current.timezone || 'Asia/Shanghai',categories:current.categories,entries}});
        accept(result);
        clearEditor();
        selectScheduleTab('view');
        toast('日程已保存');
      } catch (error) { setError('#scheduleFormError',error.message || '日程保存失败，请检查后重试。'); }
    }).catch(() => {});
  }
  function saveEntries(entries) {
    const current = scheduleData();
    return api('schedule/save',{schedule:{timezone:current.timezone || 'Asia/Shanghai',categories:current.categories,entries}}).then(result => { accept(result); return result; });
  }
  async function saveCategories(categories,entries,category_changes=null) {
    const current=scheduleData(),schedule={timezone:current.timezone || 'Asia/Shanghai',categories,entries};
    return withWriteGate(null,async()=>{const result=await api('schedule/save',{schedule,...(category_changes?{category_changes}:{})});accept(result);render();return result;});
  }
  function editCategory(name='') {
    const current=name?visibleCategories().find(item=>item===name):null;
    modal(current?'重命名日程分类':'新建日程分类',`<label>分类名称<input name="categoryName" required maxlength="80" value="${escape(current || '')}" placeholder="例如：工作安排"></label>`,async body=>{
      const next=$('input',body).value.trim(),categories=[...new Set(scheduleData().categories.map(String))],all=visibleCategories();
      if(!next)throw Error('请填写分类名称。');
      if(current && next===current)return;
      if(all.some(item=>item===next && item!==current))throw Error('已有同名分类，请换一个名称。');
      if(current){const renamed=categories.filter(item=>item!==current);if(!renamed.includes(next))renamed.push(next);const entries=scheduleData().entries.map(entry=>entry.category===current?{...entry,category:next}:entry);await saveCategories(renamed,entries,{rename:{[current]:next}});categoryFilter=next;}
      else {await saveCategories([...categories,next],scheduleData().entries);categoryFilter=next;}
      render();toast('日程分类已保存');
    },current?'保存分类':'新建分类');
  }
  function deleteCategory(name) {
    const affected=scheduleData().entries.filter(entry=>entry.category===name).length;
    modal('删除日程分类',`<p>删除“${escape(name)}”后，${affected?`${affected} 条手动日程将移至“未分类”，`:'手动日程分类将保持不变，'}生活动作的此分类也会同步移除。</p>`,async()=>{
      const categories=scheduleData().categories.filter(item=>String(item)!==name),entries=scheduleData().entries.map(entry=>entry.category===name?{...entry,category:''}:entry);
      await saveCategories(categories,entries,{delete:[name]});categoryFilter='*';render();toast('日程分类已删除');
    },'删除分类');
  }
  function confirmDelete(entry) {
    modal('删除日程',`<p>确定删除“${escape(entry.name)}”吗？删除后，这条日程将不再参与活动安排。</p>`,async () => {
      const entries = (scheduleData().entries || []).filter(item => item.id !== entry.id);
      const saved = await withWriteGate(null,async () => { await saveEntries(entries); toast('日程已删除'); return true; });
      if (!saved) throw Error('已有日程配置正在保存，请稍后重试。');
    },'删除日程');
  }
  async function handleAction(button) {
    const action = button.dataset.scheduleAction;
    if (action === 'new') { startNewSchedule(); return; }
    if (action === 'cancel') { clearEditor(); selectScheduleTab('view'); $('#scheduleViewTab').focus(); return; }
    if (action === 'remove-rate') {
      const rows = currentRateRows(); rows.splice(Number(button.closest('.schedule-rate-row').dataset.index),1); renderRateRows(rows); return;
    }
    const row = button.closest('[data-schedule]');
    const entry = (scheduleData().entries || []).find(item => item.id === row?.dataset.schedule);
    if (!entry) return;
    if (action === 'edit') { renderEditor(entry); return; }
    if (action === 'delete') { confirmDelete(entry); return; }
    if (action === 'toggle') {
      const entries = (scheduleData().entries || []).map(item => item.id === entry.id ? {...item,enabled:item.enabled === false} : item);
      let saved = false;
      await withWriteGate(button,async () => { await saveEntries(entries); saved = true; }).catch(() => {});
      if (saved) toast(entry.enabled === false ? '日程已启用' : '日程已停用');
    }
  }

  timezoneInput.addEventListener('input',() => { timezoneDirty = true; setError('#timezoneError',''); });
  timezoneForm.addEventListener('submit',saveTimezone);
  form.addEventListener('submit',saveEntry);
  form.elements.dayPreset.addEventListener('change',() => {
    const preset = form.elements.dayPreset.value;
    if (preset !== 'custom') $$('#customDays input[name="days"]').forEach(input => { input.checked = (DAY_PRESETS[preset] || []).includes(Number(input.value)); });
    $('#customDays').hidden = preset !== 'custom';
  });
  for (const field of ['start','end']) form.elements[field].addEventListener('input',updateTimeHint);
  $('#addScheduleRate').addEventListener('click',() => {
    const rows = currentRateRows();
    const used = new Set(rows.map(row => row.state));
    const next = numericStates().find(state => !used.has(state.id));
    if (!next) {
      setError('#scheduleFormError',numericStates().length ? '每个数值状态只能设置一条覆盖。' : '当前没有可配置的数值状态。');
      return;
    }
    rows.push({state:next.id,value:'0'});
    renderRateRows(rows);
    setError('#scheduleFormError','');
    $('.rate-value:last-of-type', $('#scheduleRateRows'))?.focus();
  });
  panel.addEventListener('click',event => {
    const filter=event.target.closest('[data-schedule-category-filter]');
    if(filter){categoryFilter=filter.dataset.scheduleCategoryFilter;renderCategoryControls();renderList();return;}
    const categoryAction=event.target.closest('[data-schedule-category-action]');
    if(categoryAction){const name=categoryAction.dataset.category;if(categoryAction.dataset.scheduleCategoryAction==='rename')editCategory(name);else deleteCategory(name);return;}
    const button = event.target.closest('[data-schedule-action]');
    if (button) handleAction(button).catch(error => toast(error.message,true));
  });
  $('#refreshScheduleButton').addEventListener('click',event => reload(event.currentTarget));
  $('#addScheduleCategoryButton').addEventListener('click',()=>editCategory());
  $('#retryScheduleButton').addEventListener('click',event => reload(event.currentTarget));
  $('#cancelScheduleEdit').addEventListener('click',() => handleAction({dataset:{scheduleAction:'cancel'}}));
  $('#cancelScheduleEditFooter').addEventListener('click',() => handleAction({dataset:{scheduleAction:'cancel'}}));
  $('#addScheduleButton').addEventListener('click',startNewSchedule);
  scheduleTabs[0].addEventListener('click',() => selectScheduleTab('view'));
  scheduleTabs[1].addEventListener('click',() => startNewSchedule());
  $('#scheduleTabs').addEventListener('keydown',event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();
    const selected = scheduleTabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true');
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? scheduleTabs.length - 1 : (selected + (event.key === 'ArrowRight' ? 1 : -1) + scheduleTabs.length) % scheduleTabs.length;
    if (next === 0) selectScheduleTab('view',true);
    else startNewSchedule(true);
  });

  $('#timezoneOptions').replaceChildren(...COMMON_TIMEZONES.map(value => {const option=document.createElement('option');option.value=value;return option;}));
  if (!$('#scheduleCreatePanel').hidden) renderEditor(null,false);
  render();
  return {render,loadFailed};
}
