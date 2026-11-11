import {setDetailsOpen} from './motion.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const blankGroup = () => ({
  id:'',kind:'group',type:'number',computed:true,name:'',description:'',category:'',scope:'character',
  enabled:true,inject:true,show_value:true,prompt:'{name}：{value}；{stage}。{description}',
  min:0,max:100,initial:0,stages:[],factors:[]
});
const numeric = value => value !== '' && value !== null && value !== undefined && Number.isFinite(Number(value));
const numberText = value => Number.isFinite(Number(value)) ? Number(Number(value).toFixed(2)).toLocaleString('zh-CN',{maximumFractionDigits:2}) : '—';
const makeId = () => `group_${globalThis.crypto?.randomUUID?.().replaceAll('-','').toLowerCase() || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`}`;

export function mountGroupEditor({api,accept,run,toast,getData,onSaved,escape = esc}) {
  const form = $('#groupForm');
  const factorsHost = $('#groupFactors');
  let groupDraft = blankGroup();
  let editing = false;
  let initialized = false;
  let data = getData();

  function categoryOptions(selected = '') {
    return [{id:'',name:'未分类'},...(data.categories || [])].map(item => `<option value="${escape(item.id)}" ${item.id===selected?'selected':''}>${escape(item.name)}</option>`).join('');
  }
  function compatibleSources(scope = form.elements.scope.value) {
    const allowed = scope === 'character' ? new Set(['character']) : scope === 'conversation' ? new Set(['character','conversation']) : new Set(['character','user']);
    return (data.definitions || []).filter(item => item.kind !== 'group' && item.type === 'number' && allowed.has(item.scope || 'character'));
  }
  function sourceName(id) {
    return (data.definitions || []).find(item => item.id === id)?.name || String(id || '');
  }
  function collect(strict = false) {
    const result = {...groupDraft};
    result.id = form.elements.id.value || groupDraft.id || makeId();
    result.kind = 'group'; result.type = 'number'; result.computed = true;
    result.name = form.elements.name.value.trim();
    result.description = form.elements.description.value.trim();
    result.category = form.elements.category.value;
    result.scope = form.elements.scope.value;
    result.enabled = form.elements.enabled.checked;
    result.inject = form.elements.inject.checked;
    result.show_value = form.elements.show_value.checked;
    result.prompt = form.elements.prompt.value;
    result.min = 0; result.max = 100;
    result.factors = $$('.group-factor',factorsHost).map(row => {
      const value = name => $(`[data-factor-field="${name}"]`,row)?.value ?? '';
      const factor = {state_id:value('state_id'),direction:value('direction') || 'positive'};
      for (const key of ['weight','low','high']) {
        const text = value(key);
        factor[key] = numeric(text) ? Number(text) : (strict ? NaN : null);
      }
      factor._rangeTouched = row.dataset.rangeTouched === 'true';
      return factor;
    });
    result.stages = $$('#groupStages .stage-row').map(row => {
      const read = key => $(`[data-stage-field="${key}"]`,row)?.value ?? '';
      return {name:read('name').trim(),description:read('description'),min:numeric(read('min'))?Number(read('min')):null,max:numeric(read('max'))?Number(read('max')):null};
    });
    groupDraft = result;
    return result;
  }
  function factorOptions(selected, index) {
    const used = new Set(groupDraft.factors.map((factor,i) => i === index ? '' : factor.state_id).filter(Boolean));
    const current = (data.definitions || []).find(item => item.id === selected && item.kind !== 'group');
    const options = compatibleSources().filter(item => !used.has(item.id) || item.id === selected);
    let html = `<option value="">选择基础数值状态</option>`;
    if (selected && !options.some(item => item.id === selected)) html += `<option value="${escape(selected)}" selected>来源不符合当前归属 · ${escape(current?.name || selected)}</option>`;
    return html + options.map(item => `<option value="${escape(item.id)}" ${item.id===selected?'selected':''}>${escape(item.name)} · ${escape(item.scope==='character'?'角色整体':item.scope==='conversation'?'当前会话':'与用户的关系')}</option>`).join('');
  }
  function factorHTML(factor,index) {
    const source = (data.definitions || []).find(item => item.id === factor.state_id && item.kind !== 'group');
    const bounded = source && numeric(source.min) && numeric(source.max) && Number(source.max) > Number(source.min);
    const lowHint = !source ? '先选择一个基础数值状态。' : bounded ? `自动带入来源范围 ${source.min}–${source.max}；可按实际含义调整。` : '该状态没有有限范围，请填写低值和高值评分边界。';
    return `<article class="group-factor" data-index="${index}" data-range-touched="${factor._rangeTouched?'true':'false'}">
      <div class="factor-heading"><strong>来源 ${index+1}</strong><div class="factor-order"><button type="button" class="quiet small" data-factor-action="up" aria-label="来源上移" ${index===0?'disabled':''}>上移</button><button type="button" class="quiet small" data-factor-action="down" aria-label="来源下移" ${index===groupDraft.factors.length-1?'disabled':''}>下移</button><button type="button" class="quiet small danger" data-factor-action="remove" aria-label="移除来源">移除</button></div></div>
      <div class="group-factor-grid">
        <label>基础数值状态<select data-factor-field="state_id" required>${factorOptions(factor.state_id,index)}</select></label>
        <label>低值边界<input data-factor-field="low" type="number" step="any" value="${escape(factor.low ?? '')}" required aria-describedby="factor-range-${index}"></label>
        <label>高值边界<input data-factor-field="high" type="number" step="any" value="${escape(factor.high ?? '')}" required aria-describedby="factor-range-${index}"></label>
        <label>相对权重<input data-factor-field="weight" type="number" min="0" step="any" value="${escape(factor.weight ?? 1)}" required></label>
        <label>评分方向<select data-factor-field="direction"><option value="positive" ${factor.direction!=='negative'?'selected':''}>数值越高，评分越高</option><option value="negative" ${factor.direction==='negative'?'selected':''}>数值越高，评分越低</option></select></label>
      </div><p class="help factor-range-note" id="factor-range-${index}">${escape(lowHint)}</p>
    </article>`;
  }
  function renderFactors() {
    factorsHost.innerHTML = groupDraft.factors.map(factorHTML).join('');
    $('#groupFactorsTitle').textContent = `指标来源${groupDraft.factors.length?` · ${groupDraft.factors.length}`:''}`;
    updateSourceHint();
    updatePreview();
  }
  function updateSourceHint() {
    const total = compatibleSources().length;
    const message = total < 2
      ? `当前归属下只有 ${total} 个基础数值状态。先创建至少两个基础数值状态，再添加综合指标。`
      : `可选 ${total} 个基础数值状态。综合指标只读取来源，不会修改它们。`;
    $('#groupSourceHint').textContent = message;
    $('#addGroupFactor').disabled = total < 2 || groupDraft.factors.length >= total;
  }
  function updatePreview() {
    const group = collect(false);
    const selected = group.factors.map(factor => ({factor,source:(data.definitions || []).find(item => item.id===factor.state_id && item.kind!=='group')}));
    const valid = selected.length >= 2 && selected.every(({factor,source}) => source && factor.low !== null && factor.high !== null && Number.isFinite(factor.low) && Number.isFinite(factor.high) && factor.high > factor.low && Number.isFinite(factor.weight) && factor.weight >= 0);
    const totalWeight = selected.reduce((sum,item) => sum + (Number.isFinite(item.factor.weight) && item.factor.weight >= 0 ? item.factor.weight : 0),0);
    const scopeInvalid = selected.some(({source}) => source && !compatibleSources(group.scope).some(item => item.id === source.id));
    if (!valid || totalWeight <= 0 || scopeInvalid || selected.some(item => !item.source)) {
      $('#groupPreview').innerHTML = `<div class="group-preview-head"><strong>按初始值预览</strong><span>等待有效来源</span></div><p class="help">添加至少两个不同的基础数值状态，并填写有效评分范围；总权重需要大于 0。</p>`;
      return;
    }
    const rows = selected.map(({factor,source}) => {
      const low=Number(factor.low),high=Number(factor.high),initial=Number(source.initial);
      const raw=Math.max(0,Math.min(100,(initial-low)/(high-low)*100));
      const score=factor.direction==='negative'?100-raw:raw;
      const share=Number(factor.weight)/totalWeight*100;
      const contribution=score*share/100;
      return {name:source.name,initial,score,share,contribution};
    });
    const result=rows.reduce((sum,row) => sum+row.contribution,0);
    $('#groupPreview').innerHTML = `<div class="group-preview-head"><strong>按初始值预览</strong><span><b>${escape(numberText(result))}</b> / 100</span></div><div class="group-preview-meter" aria-label="综合预览 ${escape(numberText(result))} 分"><span style="width:${Math.max(0,Math.min(100,result))}%"></span></div><div class="group-preview-rows">${rows.map(row => `<p><span>${escape(row.name)} · 初始 ${escape(numberText(row.initial))}</span><span>评分 ${escape(numberText(row.score))} × ${escape(numberText(row.share))}% · 贡献 ${escape(numberText(row.contribution))}</span></p>`).join('')}</div>`;
  }
  function renderStages() {
    $('#groupStages').innerHTML = groupDraft.stages.map((stage,index) => `<div class="stage-row" data-index="${index}"><div class="stage-fields"><label>阶段名称<input data-stage-field="name" value="${escape(stage.name || '')}" required></label><label>包含下界<input data-stage-field="min" type="number" step="any" value="${escape(stage.min ?? '')}" placeholder="不限"></label><label>不含上界<input data-stage-field="max" type="number" step="any" value="${escape(stage.max ?? '')}" placeholder="不限"></label><div class="stage-order"><button type="button" class="quiet small" data-stage-action="up" aria-label="阶段上移" ${index===0?'disabled':''}>上移</button><button type="button" class="quiet small" data-stage-action="down" aria-label="阶段下移" ${index===groupDraft.stages.length-1?'disabled':''}>下移</button><button type="button" class="quiet small danger" data-stage-action="remove" aria-label="移除阶段">移除</button></div></div><label>阶段描述<textarea data-stage-field="description" rows="2">${escape(stage.description || '')}</textarea></label></div>`).join('') || '<p class="help">尚未添加阶段。</p>';
  }
  function render() {
    if (!initialized) return;
    const current = groupDraft;
    form.reset();
    for (const key of ['id','name','description','scope','prompt']) form.elements[key].value = current[key] ?? '';
    form.elements.category.innerHTML = categoryOptions(current.category || '');
    for (const key of ['enabled','inject','show_value']) form.elements[key].checked = current[key] !== false;
    void setDetailsOpen($('#groupStagesDetails'),(current.stages || []).length > 0);
    renderFactors();
    renderStages();
    $('#groupFormError').hidden = true;
    $('#saveGroupButton').textContent = editing ? '保存综合指标' : '创建综合指标';
  }
  function newGroup() {
    groupDraft = {...blankGroup(),id:makeId()};
    editing = false; initialized = true; render();
  }
  function editGroup(group) {
    groupDraft = {...blankGroup(),...structuredClone(group),kind:'group',type:'number',computed:true,id:group.id,factors:(group.factors || []).map(factor => ({weight:1,direction:'positive',_rangeTouched:true,...structuredClone(factor)})),stages:structuredClone(group.stages || [])};
    editing = true; initialized = true; render();
  }
  function refreshOptionLists() {
    if (!initialized) return;
    const category = form.elements.category;
    if (category !== document.activeElement) {
      const markup=categoryOptions(groupDraft.category || '');
      if (category.dataset.optionMarkup !== markup) {category.innerHTML=markup;category.dataset.optionMarkup=markup;}
    }
    const available=compatibleSources();
    for (const select of $$('[data-factor-field="state_id"]',factorsHost)) {
      if (select===document.activeElement) continue;
      const row=select.closest('.group-factor'),index=Number(row?.dataset.index),current=groupDraft.factors[index]?.state_id || select.value;
      const markup=factorOptions(current,index);
      if (select.dataset.optionMarkup !== markup) {select.innerHTML=markup;select.dataset.optionMarkup=markup;select.value=current;}
    }
    updateSourceHint(); updatePreview();
  }
  function factorRowsChanged(focusIndex = -1,focusField = '') {
    groupDraft=collect(false);renderFactors();
    if (focusIndex >= 0 && focusField) $(`[data-index="${focusIndex}"] [data-factor-field="${focusField}"]`,factorsHost)?.focus({preventScroll:true});
  }
  function validate(group) {
    if (!group.name) throw Error('请填写综合指标名称。');
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(group.id)) throw Error('综合指标技术标识无效，请重新创建。');
    if (group.factors.length < 2) throw Error('综合指标至少需要两个不同的基础数值状态来源。');
    const ids = new Set();
    let totalWeight=0;
    for (const factor of group.factors) {
      if (!factor.state_id) throw Error('请选择每个来源的基础数值状态。');
      if (ids.has(factor.state_id)) throw Error('同一个基础状态不能重复作为来源。');
      ids.add(factor.state_id);
      const source=(data.definitions || []).find(item => item.id===factor.state_id && item.kind!=='group');
      if (!source || source.type!=='number') throw Error('综合指标来源必须是仍存在的基础数值状态。');
      if (!compatibleSources(group.scope).some(item => item.id===source.id)) throw Error(`“${source.name}”的归属不适用于当前综合指标归属。`);
      if (!Number.isFinite(factor.low) || !Number.isFinite(factor.high) || factor.high<=factor.low) throw Error(`“${source.name}”需要有效的低值和高值评分边界，且高值必须大于低值。`);
      if (!Number.isFinite(factor.weight) || factor.weight<0) throw Error(`“${source.name}”的相对权重必须是大于或等于 0 的数值。`);
      totalWeight+=factor.weight;
    }
    if (totalWeight<=0) throw Error('来源权重总和必须大于 0。');
  }
  function moveIn(list,index,direction) {
    const next=index+direction;
    if (next<0 || next>=list.length) return;
    [list[index],list[next]]=[list[next],list[index]];
  }

  $('#addGroupFactor').onclick=() => {
    groupDraft=collect(false);
    groupDraft.factors.push({state_id:'',weight:1,low:null,high:null,direction:'positive'});
    renderFactors();
    const index=groupDraft.factors.length-1;
    $(`[data-index="${index}"] [data-factor-field="state_id"]`,factorsHost)?.focus({preventScroll:true});
  };
  form.elements.scope.addEventListener('change',() => {groupDraft=collect(false);refreshOptionLists();});
  form.addEventListener('input',event => {
    $('#groupFormError').hidden=true;
    if (event.target.closest('#groupFactors')) {
      groupDraft=collect(false);updatePreview();
    } else if (event.target.matches('[name="name"],[name="description"]')) groupDraft=collect(false);
  });
  form.addEventListener('change',event => {
    $('#groupFormError').hidden=true;
    const select=event.target.closest('[data-factor-field="state_id"]');
    if (select) {
      const row=select.closest('.group-factor'),index=Number(row.dataset.index),old=groupDraft.factors[index] || {};
      const source=(data.definitions || []).find(item => item.id===select.value && item.kind!=='group');
      const bounded=source && numeric(source.min) && numeric(source.max) && Number(source.max)>Number(source.min);
      groupDraft=collect(false);
      const factor=groupDraft.factors[index];
      if (factor) {
        factor.state_id=select.value;
        const touched=old._rangeTouched || row.dataset.rangeTouched==='true';
        factor._rangeTouched=touched;
        if (!touched && bounded) {factor.low=Number(source.min);factor.high=Number(source.max);}
        else if (!touched) {factor.low=null;factor.high=null;}
        else {factor.low=old.low ?? factor.low;factor.high=old.high ?? factor.high;}
      }
      renderFactors();
      $(`[data-index="${index}"] [data-factor-field="state_id"]`,factorsHost)?.focus({preventScroll:true});
      return;
    }
    if (event.target.matches('[name="category"]')) groupDraft=collect(false);
  });
  factorsHost.addEventListener('input',event => {
    if (event.target.matches('[data-factor-field="low"],[data-factor-field="high"]')) event.target.closest('.group-factor').dataset.rangeTouched='true';
  });
  factorsHost.addEventListener('click',event => {
    const button=event.target.closest('[data-factor-action]');if (!button) return;
    groupDraft=collect(false);
    const index=Number(button.closest('.group-factor').dataset.index),action=button.dataset.factorAction;
    if (action==='remove') groupDraft.factors.splice(index,1);
    if (action==='up') moveIn(groupDraft.factors,index,-1);
    if (action==='down') moveIn(groupDraft.factors,index,1);
    renderFactors();
  });
  $('#addGroupStage').onclick=() => {
    groupDraft=collect(false);groupDraft.stages.push({name:'',description:'',min:null,max:null});
    renderStages();void setDetailsOpen($('#groupStagesDetails'),true);
  };
  $('#groupStages').addEventListener('input',() => {groupDraft=collect(false);});
  $('#groupStages').addEventListener('click',event => {
    const button=event.target.closest('[data-stage-action]');if(!button)return;
    groupDraft=collect(false);const index=Number(button.closest('.stage-row').dataset.index),action=button.dataset.stageAction;
    if(action==='remove')groupDraft.stages.splice(index,1);
    if(action==='up')moveIn(groupDraft.stages,index,-1);
    if(action==='down')moveIn(groupDraft.stages,index,1);
    renderStages();void setDetailsOpen($('#groupStagesDetails'),groupDraft.stages.length>0);
  });
  document.addEventListener('focusout',event => {
    if (event.target.closest('#groupForm')) refreshOptionLists();
  });
  form.onsubmit=async event => {
    event.preventDefault();
    const button=$('#saveGroupButton');button.disabled=true;$('#groupFormError').hidden=true;
    try {
      const group=collect(true);validate(group);
      group.factors=group.factors.map(({_rangeTouched,weight_percent,...factor}) => factor);
      accept(await api('groups/save',{group,create:!editing}));
      toast('综合指标已保存');
      initialized=false;groupDraft=blankGroup();editing=false;
      onSaved?.();
    } catch(error) {$('#groupFormError').textContent=error.message;$('#groupFormError').hidden=false;}
    finally {button.disabled=false;}
  };

  return {
    newGroup,editGroup,collect,setData(next){data=next || getData();refreshOptionLists();},render,reset(){initialized=true;editing=false;groupDraft={...blankGroup(),id:makeId()};render();},
    isEditing:() => editing,
    getDraft:() => groupDraft
  };
}
