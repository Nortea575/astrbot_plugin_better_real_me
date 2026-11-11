import {switchPanels} from './motion.js';

const $ = (selector,root=document) => root.querySelector(selector);
const $$ = (selector,root=document) => [...root.querySelectorAll(selector)];
const ROOM_WIDTH=178,LAYER_GAP=78,ROOM_GAP=24,COMPONENT_GAP=72,AVATAR_SIZE=22,AVATAR_GAP=5,MIN_INITIAL_MAP_LABEL_PX=11;

export function mountIndoor({api,accept,run,toast,modal,getData,getActor,onContextChange,refresh,escape}) {
  const tabs=[$('#indoorViewTab'),$('#indoorCreateTab')],panels=[$('#indoorViewPanel'),$('#indoorCreatePanel')];
  const roomForm=$('#indoorRoomForm'),groupForm=$('#indoorGroupForm'),initialForm=$('#initialRoomForm'),connectionForm=$('#connectionForm');
  let editingRoomId='',editingGroupId='',editingKind='room',roomDraft={id:'',name:'',description:'',group_id:'',initial:false},groupDraft={id:'',name:'',description:'',parent_id:'',entry_room_id:''};
  let roomFormInitialized=false,groupFormInitialized=false,editorDirty=false,initialDraft='',initialDirty=false,connectionDraft={a:'',b:''};
  let currentGroupId='',selectedNodeKey='',selectedActor=null,route=null,routeError='',routeSequence=0,graphKey='',dataActorKey='',contextSwitching=true;
  let indoorWriteBusy=false,moveBusy=false,notice=null,map={width:800,height:360,nodes:new Map(),edges:[]},mapScale=1,mapFit=null,pendingFocusNodeKey='',pointerDrag=null,initialLifeSelectionRequested=false;

  const esc=value=>escape(String(value ?? ''));
  const rawIndoor=()=>getData().indoor || {};
  function data() {
    const raw=rawIndoor(),rooms=Array.isArray(raw.rooms)?raw.rooms:[],groups=Array.isArray(raw.groups)?raw.groups:[];
    const rootId=String(raw.root_group_id || groups.find(group=>!group.parent_id)?.id || 'group_root');
    const normalizedGroups=groups.length?groups:[{id:rootId,name:'街区',description:'',parent_id:'',entry_room_id:''}];
    return {...raw,schema_version:2,root_group_id:rootId,groups:normalizedGroups,rooms:rooms.map(room=>({...room,group_id:room.group_id || rootId})),connections:Array.isArray(raw.connections)?raw.connections:[],initial_room_id:raw.initial_room_id || ''};
  }
  const rawRuntime=()=>getData().indoor_runtime || {room_id:'',occupants:[],occupancy:{}};
  const rootId=()=>data().root_group_id;
  const groupById=id=>data().groups.find(group=>String(group.id)===String(id)) || null;
  const roomById=id=>data().rooms.find(room=>String(room.id)===String(id)) || null;
  const childGroups=id=>data().groups.filter(group=>String(group.parent_id || '')===String(id));
  const childRooms=id=>data().rooms.filter(room=>String(room.group_id || '')===String(id));
  const normalizeContext=value=>value && value.character!=null && value.conversation!=null && value.user!=null
    ? {character:String(value.character),conversation:String(value.conversation),user:String(value.user)}:null;
  const normalizeActor=value=>{
    const context=normalizeContext(value?.context) || normalizeContext(value);
    if(context)return {...value,context};
    return value?.life && value.account_id!=null?{...value,context:null,account_id:String(value.account_id)}:null;
  };
  const availableActors=()=> (getData().actors || []).map(normalizeActor).filter(Boolean);
  const actorKey=value=>{const actor=normalizeActor(value);return actor?.context?`${actor.context.character}\u001f${actor.context.conversation}\u001f${actor.context.user}`:actor?.account_id?`life:${actor.account_id}`:'';};
  const actorSelection=value=>{const actor=normalizeActor(value);return actor?.context?{context:actor.context}:actor?.account_id?{account_id:actor.account_id}:{};};
  const actorRouteSelection=value=>{const actor=normalizeActor(value),account=(getData().life?.accounts || []).find(item=>item.id===actor?.account_id && item.enabled);return actor?.life || account?{account_id:actor.account_id}:actor?.context?{context:actor.context}:{};};
  const occupants=()=>rawRuntime().occupants || [];
  const occupantsForRoom=id=>occupants().filter(person=>person.room_id===id).sort((a,b)=>String(a.name || '').localeCompare(String(b.name || ''),'zh-CN') || String(a.character || '').localeCompare(String(b.character || '')));
  const occupantsForActor=actor=>{
    const account=(getData().life?.accounts || []).find(item=>item.id===actor?.account_id && item.enabled);
    return account?occupants().find(person=>person.life && person.account_id===account.id) || occupants().find(person=>person.character===actor.character):occupants().find(person=>person.character===actor?.context?.character || person.character===actor?.character);
  };
  const currentRoomId=()=>selectedActor?occupantsForActor(selectedActor)?.room_id || rawRuntime().room_id || '':rawRuntime().room_id || '';
  const currentActivity=()=>{const account=(getData().life?.accounts || []).find(item=>item.id===selectedActor?.account_id && item.enabled);return occupantsForActor(selectedActor)?.current_activity || account?.current_activity || null;};
  const nodeKey=(kind,id)=>`${kind}:${id}`;
  const parseNodeKey=value=>{const text=String(value || ''),separator=text.indexOf(':');return separator<0?null:{kind:text.slice(0,separator),id:text.slice(separator+1)};};
  const sortNamed=items=>[...items].sort((a,b)=>String(a.name || '').localeCompare(String(b.name || ''),'zh-CN') || String(a.id).localeCompare(String(b.id)));
  const edgeEnds=(a,b)=>String(a)<String(b)?[String(a),String(b)]:[String(b),String(a)];
  const edgeKey=(a,b)=>edgeEnds(a,b).join('\u001f');

  function groupAncestors(id) {
    const result=[],seen=new Set();let group=groupById(id);
    while(group && !seen.has(String(group.id))) {seen.add(String(group.id));result.unshift(group);group=group.parent_id?groupById(group.parent_id):null;}
    return result;
  }
  function roomAncestors(roomOrId) {const room=typeof roomOrId==='object'?roomOrId:roomById(roomOrId);return room?groupAncestors(room.group_id):[];}
  function roomPath(roomOrId) {const room=typeof roomOrId==='object'?roomOrId:roomById(roomOrId);return room?[...roomAncestors(room),room].map(item=>item.name || item.id):[];}
  function roomPathLabel(roomOrId) {return roomPath(roomOrId).join(' / ') || '尚未分配房间';}
  function descendantRooms(id) {
    const result=[],visited=new Set();
    const visit=groupId=>{if(visited.has(String(groupId)))return;visited.add(String(groupId));result.push(...childRooms(groupId));for(const child of childGroups(groupId))visit(child.id);};
    visit(id);return result;
  }
  function roomBelongsToGroup(roomId,groupId) {return Boolean(roomId && groupId && descendantRooms(groupId).some(room=>String(room.id)===String(roomId)));}
  function descendantGroupIds(id) {const result=new Set(),visit=groupId=>{for(const group of childGroups(groupId)){if(result.has(String(group.id)))continue;result.add(String(group.id));visit(group.id);}};visit(id);return result;}
  function visibleNodeForRoom(roomId,groupId=currentGroupId) {
    const room=roomById(roomId);if(!room)return null;
    if(String(room.group_id)===String(groupId))return {kind:'room',id:room.id,key:nodeKey('room',room.id)};
    let group=groupById(room.group_id),seen=new Set();
    while(group && String(group.parent_id || '')!==String(groupId) && !seen.has(String(group.id))) {seen.add(String(group.id));group=group.parent_id?groupById(group.parent_id):null;}
    return group && String(group.parent_id || '')===String(groupId)?{kind:'group',id:group.id,key:nodeKey('group',group.id)}:null;
  }
  function visibleNodes(groupId=currentGroupId) {
    const rooms=sortNamed(childRooms(groupId)).map(room=>({kind:'room',id:room.id,key:nodeKey('room',room.id),item:room}));
    const groups=sortNamed(childGroups(groupId)).map(group=>({kind:'group',id:group.id,key:nodeKey('group',group.id),item:group}));
    return [...rooms,...groups];
  }
  function nodeTitle(kind,id) {return (kind==='group'?groupById(id):roomById(id))?.name || String(id || '未命名空间');}
  function roomTitle(id) {return roomById(id)?.name || String(id || '尚未分配房间');}
  function targetNode() {const parsed=parseNodeKey(selectedNodeKey);if(!parsed)return null;const {kind,id}=parsed;return {kind,id,key:selectedNodeKey,item:kind==='group'?groupById(id):roomById(id)};}
  function runtime() {
    const room_id=currentRoomId(),room=roomById(room_id),group=room?groupById(room.group_id):null;
    const exits=(data().connections || []).filter(edge=>edge.a===room_id || edge.b===room_id).map(edge=>roomById(edge.a===room_id?edge.b:edge.a)).filter(Boolean);
    return {...rawRuntime(),room_id,room,group,exits,location_path:roomPath(room_id)};
  }
  function setSelectedForCurrent() {const current=currentRoomId(),visible=visibleNodeForRoom(current);if(visible){selectedNodeKey=visible.key;pendingFocusNodeKey=visible.key;} }

  function selectTab(which,focus=false) {
    if(which==='create')prepareNewDraft();
    const index=which==='create'?1:0;
    tabs.forEach((tab,i)=>{tab.setAttribute('aria-selected',String(i===index));tab.tabIndex=i===index?0:-1;});
    switchPanels(panels,panels[index]);if(focus)tabs[index].focus();
  }
  function setEditorKind(kind) {
    const isGroup=kind==='group';$('#indoorCreateKind').value=kind;roomForm.hidden=isGroup;groupForm.hidden=!isGroup;
    $('#saveIndoorRoomButton').disabled=indoorWriteBusy;$('#saveIndoorGroupButton').disabled=indoorWriteBusy;
  }
  function prepareNewDraft() {
    if(editingRoomId || editingGroupId || roomFormInitialized || groupFormInitialized)return;
    setEditorKind($('#indoorCreateKind').value || 'room');
    roomForm.reset();groupForm.reset();
    roomDraft={id:'',name:'',description:'',group_id:currentGroupId || rootId(),initial:data().rooms.length===0};
    groupDraft={id:'',name:'',description:'',parent_id:currentGroupId || rootId(),entry_room_id:''};
    $('#indoorRoomId').value='';$('#indoorRoomName').value='';$('#indoorRoomDescription').value='';$('#indoorSetInitial').checked=roomDraft.initial;
    $('#indoorGroupId').value='';$('#indoorGroupName').value='';$('#indoorGroupDescription').value='';
    $('#indoorRoomFormError').hidden=true;$('#indoorGroupFormError').hidden=true;
    $('#indoorEditorTitle').textContent='新建空间';$('#indoorEditorHint').textContent='选择创建房间或房间组。';
    roomFormInitialized=true;groupFormInitialized=true;editorDirty=false;renderEditorOptions();syncWriteControls();
  }
  function invalidateRoute() {routeSequence++;route=null;routeError='';}
  function contextLabel(value) {const actor=normalizeActor(value),name=String(actor?.name || '').trim(),qq=String(actor?.qq || '').trim();if(name)return qq?`${name} · QQ ${qq}`:name;if(qq)return `QQ ${qq}`;return actor?.context?String(actor.context.character).split(':').slice(-1)[0]:'真实 QQ';}
  function placeChildren(parent,nodes) {let cursor=parent.firstElementChild;for(const node of nodes){if(node===cursor)cursor=cursor.nextElementSibling;else parent.insertBefore(node,cursor);}const keep=new Set(nodes);for(const child of [...parent.children])if(!keep.has(child))child.remove();}
  function parseAccountNode(html) {const template=document.createElement('template');template.innerHTML=html.trim();const node=template.content.firstElementChild;node.dataset.renderHTML=html;return node;}
  function renderAccounts() {
    const known=getData().accounts || [],lifeAccounts=getData().life?.accounts || [],keyOf=account=>`${account.platform_id || 'qq'}:${account.qq || ''}`;
    const lifeByKey=new Map(lifeAccounts.map(account=>[keyOf(account),account])),accounts=known.map(account=>({...account,lifeAccount:lifeByKey.get(keyOf(account)) || null})),seen=new Set(accounts.map(keyOf));
    for(const account of lifeAccounts)if(!seen.has(keyOf(account))){accounts.push({...account,lifeAccount:account});seen.add(keyOf(account));}
    $('#qqAccountCount').textContent=String(accounts.length);const parent=$('#qqAccountList');
    if(!accounts.length){const html='<p class="help">等待真实 QQ 连接后自动识别账号。</p>';if(parent.dataset.renderHTML!==html){parent.innerHTML=html;parent.dataset.renderHTML=html;}return;}
    parent.dataset.renderHTML='';const existing=new Map($$('.qq-account',parent).map(node=>[node.dataset.accountKey,node])),ordered=[];
    for(const account of accounts){
      const key=String(account.id || keyOf(account)),name=String(account.lifeAccount?.name || account.name || `QQ ${account.qq || ''}`),qq=String(account.qq || ''),avatar=String(account.lifeAccount?.avatar_url || account.avatar_url || ''),initial=Array.from(name.trim())[0] || Array.from(qq)[0] || '?';
      const life=account.lifeAccount,occupant=occupants().find(person=>person.life && (person.account_id===life?.id || person.qq===qq)),roomId=occupant?.room_id || life?.current_activity?.room_id || life?.current_room_id || '',room=roomId?roomPathLabel(roomId):'';
      const activity=life?.current_activity,chatStatus=account.context?'已建立真实会话归属':'等待真实会话建立归属',configured=Boolean(life?.enabled || life?.preset_configured || life?.preset_id);
      const summary=life?.enabled?`生活全局 · ${[room,activity?.name].filter(Boolean).join(' · ') || '已启用'} · 自动行为已启用`:life?`${configured?`生活预设已暂停${room?` · ${room}`:''}`:'生活预设未启用 · 默认关闭'} · ${chatStatus}`:account.context?chatStatus:'账号已识别，等待真实会话建立归属';
      const html=`<article class="qq-account" data-account-key="${esc(key)}"><span class="qq-account-avatar">${avatar?`<img src="${esc(avatar)}" alt="" loading="lazy"><span hidden>${esc(initial)}</span>`:`<span>${esc(initial)}</span>`}</span><span class="qq-account-copy"><strong>${esc(name)}</strong><span>QQ ${esc(qq || '—')}</span><small>${esc(summary)}</small></span></article>`;
      let node=existing.get(key);if(!node)node=parseAccountNode(html);else if(node.dataset.renderHTML!==html){node.innerHTML=parseAccountNode(html).innerHTML;node.dataset.renderHTML=html;}if(!node.dataset.renderHTML)node.dataset.renderHTML=html;ordered.push(node);
    }
    placeChildren(parent,ordered);parent.querySelectorAll('.qq-account-avatar img').forEach(image=>{if(image.dataset.fallbackReady)return;image.dataset.fallbackReady='true';image.addEventListener('error',()=>{image.hidden=true;image.nextElementSibling.hidden=false;},{once:true});});
  }
  function renderContexts() {
    const actors=availableActors();if(selectedActor)selectedActor=actors.find(item=>actorKey(item)===actorKey(selectedActor)) || null;
    if(!selectedActor && actors.length){const requested=normalizeContext(getData().context) || normalizeContext(getActor()),selectedAccountId=String(getData().selected_account_id || '');selectedActor=actors.find(item=>item.account_id===selectedAccountId) || actors.find(item=>requested && item.context?.character===requested.character) || actors.find(item=>item.life) || actors[0];}
    const key=actorKey(selectedActor);if(key!==dataActorKey){contextSwitching=true;invalidateRoute();}
    const select=$('#indoorContextSelect'),label=$('label[for="indoorContextSelect"]');
    const markup=actors.map(actor=>`<option value="${esc(JSON.stringify(actorSelection(actor)))}" ${actorKey(actor)===key?'selected':''}>${esc(contextLabel(actor))}</option>`).join('');
    if(select!==document.activeElement && select.dataset.actorOptions!==markup){select.innerHTML=markup;select.dataset.actorOptions=markup;if(selectedActor)select.value=JSON.stringify(actorSelection(selectedActor));}
    const hasActors=actors.length>0,lifeOccupants=occupants().filter(person=>person.life && person.room_id),hasChatActors=actors.some(item=>item.context);
    if(label)label.hidden=!hasActors;select.hidden=!hasActors;select.disabled=!hasActors || actors.length<2 || contextSwitching || indoorWriteBusy || moveBusy;
    $('#indoorContextHint').textContent=hasActors?selectedActor?.life && !hasChatActors?'生活 QQ 的全局状态和房间位置已显示；按聊天或用户归属的状态仍等待真实聊天。':'房间位置按真实角色共享，跨会话保持一致。':lifeOccupants.length?'已启用生活 QQ 会显示全局状态和房间位置；按会话或用户归属的状态仍等待真实聊天。':(getData().accounts || []).length?'账号已识别，等待真实会话建立归属；房间配置仍可管理。':'等待真实 QQ 账号连接；房间配置仍可管理。';
    renderAccounts();syncWriteControls();renderRouteControls();
  }
  async function switchContext(selection) {
    const requestedContext=normalizeContext(selection?.context || selection),requestedAccount=String(selection?.account_id || ''),actors=availableActors();
    const nextActor=requestedAccount?actors.find(item=>item.account_id===requestedAccount):actors.find(item=>requestedContext && actorKey(item)===actorKey(requestedContext));
    if(!nextActor || indoorWriteBusy || moveBusy || contextSwitching){toast('环境操作正在处理中，请稍后切换角色。',true);return;}if(actorKey(nextActor)===actorKey(selectedActor))return;
    const previous=selectedActor;selectedActor=nextActor;contextSwitching=true;invalidateRoute();setSelectedForCurrent();render();
    try{await onContextChange(actorSelection(nextActor));}catch(error){selectedActor=previous;contextSwitching=false;routeError=error.message || '角色位置未能加载，请重试。';render();toast(routeError,true);}
  }
  function renderNotice() {const node=$('#indoorNotice');node.hidden=!notice;node.classList.toggle('error',Boolean(notice?.error));node.textContent=notice?.message || '';}
  function groupTitlePath(id) {return groupAncestors(id).map(group=>group.name || group.id);}
  function allRoomLabel(room) {return [...groupTitlePath(room.group_id),room.name || room.id].join(' / ');}
  function renderBreadcrumbs() {
    const host=$('#indoorBreadcrumbs'),path=groupAncestors(currentGroupId || rootId());
    const markup=path.map((group,index)=>`${index?'<span aria-hidden="true">/</span>':''}<button type="button" class="quiet small" data-go-group="${esc(group.id)}" ${index===path.length-1?'aria-current="page"':''}>${esc(group.name || group.id)}</button>`).join('');
    if(host.dataset.renderMarkup!==markup){host.innerHTML=markup || '<span>街区</span>';host.dataset.renderMarkup=markup;}
  }
  function renderInitialOptions() {
    const rooms=sortNamed(data().rooms),select=$('#indoorInitialRoom');if(select===document.activeElement)return;
    const wanted=initialDirty?initialDraft:data().initial_room_id,markup=!rooms.length?'<option value="">尚无房间</option>':`<option value="">未设置</option>${rooms.map(room=>`<option value="${esc(room.id)}">${esc(allRoomLabel(room))}</option>`).join('')}`;
    if(select.dataset.optionMarkup!==markup){select.innerHTML=markup;select.dataset.optionMarkup=markup;}
    if(!rooms.length){initialDraft='';initialDirty=false;select.value='';return;}
    if(wanted && rooms.some(room=>String(room.id)===String(wanted)))select.value=wanted;else if(initialDirty){initialDraft=data().initial_room_id;initialDirty=false;select.value=initialDraft;}else select.value=data().initial_room_id || '';
  }
  function nodeOccupants(node) {if(node.kind==='room')return occupantsForRoom(node.id);const ids=new Set(descendantRooms(node.id).map(room=>room.id));return occupants().filter(person=>ids.has(person.room_id)).sort((a,b)=>String(a.name || '').localeCompare(String(b.name || ''),'zh-CN') || String(a.character || '').localeCompare(String(b.character || '')));}
  function projectedEdges(groupId=currentGroupId) {
    const groups=new Map();
    for(const edge of data().connections){const first=visibleNodeForRoom(edge.a,groupId),second=visibleNodeForRoom(edge.b,groupId);if(!first || !second || first.key===second.key)continue;const [a,b]=[first.key,second.key].sort(),key=`${a}\u001f${b}`;if(!groups.has(key))groups.set(key,{a,b,key,items:[]});groups.get(key).items.push(edge);}
    return [...groups.values()].sort((a,b)=>a.key.localeCompare(b.key));
  }
  function graphSignature() {return JSON.stringify({rooms:data().rooms.map(room=>[room.id,room.name,room.group_id]),groups:data().groups.map(group=>[group.id,group.name,group.parent_id,group.entry_room_id]),connections:data().connections,initial:data().initial_room_id});}
  function nodeHeight(node) {const rows=Math.ceil(nodeOccupants(node).length/Math.max(1,Math.floor((ROOM_WIDTH-20)/(AVATAR_SIZE+AVATAR_GAP))));return Math.max(84,62+rows*25);}
  function layoutGraph() {
    const items=visibleNodes(),byKey=new Map(items.map(node=>[node.key,node])),edges=projectedEdges(),neighbors=new Map(items.map(node=>[node.key,new Set()]));
    for(const edge of edges){if(!neighbors.has(edge.a)||!neighbors.has(edge.b))continue;neighbors.get(edge.a).add(edge.b);neighbors.get(edge.b).add(edge.a);}
    const currentVisible=visibleNodeForRoom(currentRoomId()),initialVisible=visibleNodeForRoom(data().initial_room_id),roots=[];
    for(const preferred of [currentVisible?.key,initialVisible?.key])if(preferred && byKey.has(preferred) && !roots.includes(preferred))roots.push(preferred);
    for(const node of items)if(!roots.includes(node.key))roots.push(node.key);
    const visited=new Set(),components=[];
    for(const root of roots){if(visited.has(root))continue;const levels=new Map([[root,0]]),queue=[root];visited.add(root);
      for(let i=0;i<queue.length;i++)for(const next of [...neighbors.get(queue[i])].sort((a,b)=>String(byKey.get(a)?.item.name).localeCompare(String(byKey.get(b)?.item.name),'zh-CN'))){if(visited.has(next))continue;visited.add(next);levels.set(next,levels.get(queue[i])+1);queue.push(next);}
      const layers=[];for(const key of queue)(layers[levels.get(key)] ||= []).push(key);for(const layer of layers)layer.sort((a,b)=>String(byKey.get(a)?.item.name).localeCompare(String(byKey.get(b)?.item.name),'zh-CN'));components.push({root,layers});
    }
    if(!items.length)return {width:800,height:360,nodes:new Map(),edges:[],components:[]};
    const maxHeight=Math.max(...components.map(component=>Math.max(...component.layers.map(layer=>layer.reduce((sum,key)=>sum+nodeHeight(byKey.get(key)),0)+Math.max(0,layer.length-1)*ROOM_GAP))));
    const height=Math.max(300,maxHeight+96);let x=36;const nodes=new Map(),labels=[];
    for(const [componentIndex,component] of components.entries()){const columns=component.layers.length,componentWidth=columns*ROOM_WIDTH+Math.max(0,columns-1)*LAYER_GAP+54;
      for(let level=0;level<columns;level++){const layer=component.layers[level],layerHeight=layer.reduce((sum,key)=>sum+nodeHeight(byKey.get(key)),0)+Math.max(0,layer.length-1)*ROOM_GAP;let y=(height-layerHeight)/2;
        for(const key of layer){const node=byKey.get(key);nodes.set(key,{...node,x:x+level*(ROOM_WIDTH+LAYER_GAP),y,height:nodeHeight(node),component:componentIndex,level});y+=nodeHeight(node)+ROOM_GAP;}}
      labels.push({x,y:22,text:component.layers.length>1?'已连接':'独立空间'});x+=componentWidth+COMPONENT_GAP;
    }
    return {width:Math.max(800,x-COMPONENT_GAP+36),height,nodes,edges,components:labels};
  }
  function edgePath(a,b) {const from=map.nodes.get(a),to=map.nodes.get(b);if(!from || !to)return '';const x1=from.x+ROOM_WIDTH/2,y1=from.y+from.height/2,x2=to.x+ROOM_WIDTH/2,y2=to.y+to.height/2,dx=x2-x1;if(Math.abs(dx)<6){const bend=Math.max(28,Math.abs(y2-y1)*.34),direction=y2>=y1?1:-1;return `M ${x1} ${y1} C ${x1+52} ${y1+direction*bend}, ${x2+52} ${y2-direction*bend}, ${x2} ${y2}`;}const curve=Math.max(34,Math.abs(dx)*.42);return `M ${x1} ${y1} C ${x1+(dx>0?curve:-curve)} ${y1}, ${x2-(dx>0?curve:-curve)} ${y2}, ${x2} ${y2}`;}
  function routeNodeKeys() {const keys=new Set();for(const id of route?.path || []){const node=visibleNodeForRoom(id);if(node)keys.add(node.key);}return keys;}
  function initializeMapFit() {if(mapFit!==null)return;const viewport=$('#indoorMapViewport'),label=$('#indoorMap .room-name');if(!viewport?.clientWidth || !viewport.clientHeight || !label || !map.width || !map.height)return;const fontSize=parseFloat(getComputedStyle(label).fontSize)||15,fitScale=Math.min(viewport.clientWidth/map.width,viewport.clientHeight/map.height);mapFit=fontSize*fitScale>=MIN_INITIAL_MAP_LABEL_PX;}
  function applyMapTransform() {const svg=$('#indoorMap'),viewport=$('#indoorMapViewport');if(!svg || !viewport || svg.hasAttribute('hidden'))return;const fitScale=mapFit?Math.min(viewport.clientWidth/map.width,viewport.clientHeight/map.height):mapScale;svg.style.width=`${Math.max(1,map.width*fitScale)}px`;svg.style.height=`${Math.max(1,map.height*fitScale)}px`;}
  function focusPendingNode() {if(!pendingFocusNodeKey)return;const viewport=$('#indoorMapViewport'),target=$(`[data-node-key="${CSS.escape(pendingFocusNodeKey)}"]`,$('#indoorMap'));if(!viewport.clientWidth || !viewport.clientHeight || !target)return;target.focus({preventScroll:true});const vr=viewport.getBoundingClientRect(),tr=target.getBoundingClientRect();viewport.scrollLeft=Math.max(0,viewport.scrollLeft+tr.left-vr.left-(viewport.clientWidth-tr.width)/2);viewport.scrollTop=Math.max(0,viewport.scrollTop+tr.top-vr.top-(viewport.clientHeight-tr.height)/2);pendingFocusNodeKey='';}
  function renderMap() {
    const svg=$('#indoorMap'),empty=$('#indoorMapEmpty'),items=visibleNodes();$('#indoorMapTitle').textContent=groupById(currentGroupId)?.name || '街区';
    if(!items.length){svg.toggleAttribute('hidden',true);empty.hidden=false;return;}empty.hidden=true;svg.toggleAttribute('hidden',false);
    map=layoutGraph();svg.setAttribute('viewBox',`0 0 ${map.width} ${map.height}`);svg.setAttribute('width',map.width);svg.setAttribute('height',map.height);
    const highlighted=routeNodeKeys(),currentNode=visibleNodeForRoom(currentRoomId()),initialNode=visibleNodeForRoom(data().initial_room_id);
    const edges=map.edges.map(edge=>`<path class="map-edge ${route?.path?.some((id,index)=>index>0 && edgeKey(visibleNodeForRoom(route.path[index-1])?.key || '',visibleNodeForRoom(id)?.key || '')===edge.key)?'route':''}" d="${edgePath(edge.a,edge.b)}" aria-hidden="true"></path>`).join('');
    const labels=map.components.map(label=>`<text class="component-label" x="${label.x}" y="${label.y}">${esc(label.text)}</text>`).join('');
    const nodes=[...map.nodes.values()].map((point,index)=>{
      const {kind,id,key,item}=point,people=nodeOccupants(point),isCurrent=currentNode?.key===key,isInitial=initialNode?.key===key,isSelected=selectedNodeKey===key,height=point.height,columns=Math.max(1,Math.floor((ROOM_WIDTH-20)/(AVATAR_SIZE+AVATAR_GAP)));
      const avatars=people.map((person,avatarIndex)=>{const row=Math.floor(avatarIndex/columns),rowStart=row*columns,count=Math.min(columns,people.length-rowStart),rowWidth=count*AVATAR_SIZE+Math.max(0,count-1)*AVATAR_GAP,x=point.x+(ROOM_WIDTH-rowWidth)/2+(avatarIndex-rowStart)*(AVATAR_SIZE+AVATAR_GAP),y=point.y+30+row*25,cx=x+AVATAR_SIZE/2,cy=y+AVATAR_SIZE/2,clip=`indoor-avatar-${index}-${avatarIndex}`,name=String(person.name || 'QQ角色'),qq=String(person.qq || ''),activity=person.current_activity?.name || '',title=`${name}${qq?` · QQ ${qq}`:''} · ${roomPathLabel(person.room_id)}${activity?` · ${activity}`:''}`,initial=Array.from(name.trim())[0] || Array.from(qq)[0] || '?',selected=actorKey(person)===actorKey(selectedActor),image=person.avatar_url?`<image class="room-avatar-image" href="${esc(person.avatar_url)}" x="${x}" y="${y}" width="${AVATAR_SIZE}" height="${AVATAR_SIZE}" clip-path="url(#${clip})" preserveAspectRatio="xMidYMid slice" aria-hidden="true"></image>`:'';
        return `<g class="room-avatar ${selected?'selected-actor':''}" role="img" aria-label="${esc(title)}"><title>${esc(title)}</title><defs><clipPath id="${clip}"><circle cx="${cx}" cy="${cy}" r="${AVATAR_SIZE/2}"></circle></clipPath></defs><circle class="room-avatar-background" cx="${cx}" cy="${cy}" r="${AVATAR_SIZE/2}"></circle>${image}<g class="room-avatar-fallback" visibility="${image?'hidden':'visible'}"><circle class="room-avatar-fallback-bg" cx="${cx}" cy="${cy}" r="${AVATAR_SIZE/2}"></circle><text class="room-avatar-initial" x="${cx}" y="${cy+4}" text-anchor="middle">${esc(initial)}</text></g><circle class="room-avatar-ring" cx="${cx}" cy="${cy}" r="${AVATAR_SIZE/2}" vector-effect="non-scaling-stroke"></circle></g>`;}).join('');
      const marker=isInitial?`<g transform="translate(${point.x+ROOM_WIDTH-52} ${point.y+8})"><rect class="room-mark" x="0" y="0" width="44" height="18" rx="9"></rect><text class="room-mark-text" x="22" y="13" text-anchor="middle">初始</text></g>`:'';
      const full=String(item.name || id),shown=Array.from(full).length>10?`${Array.from(full).slice(0,9).join('')}…`:full,typeLabel=kind==='group'?'房间组':'房间',status=[isCurrent?'当前角色所在位置':'',isInitial?'新角色初始房间':'',isSelected?'已选中':''].filter(Boolean).join('、') || '选择查看详情';
      const subtitle=kind==='group'?'进入查看下级空间':`${people.length} 位真实角色`;
      return `<g class="room-node map-node ${kind==='group'?'group-node':''} ${isCurrent?'current':''} ${isInitial?'initial':''} ${isSelected?'selected':''} ${highlighted.has(key)?'on-route':''}" data-node-key="${esc(key)}" data-node-kind="${kind}" data-node-id="${esc(id)}" ${kind==='room'?`data-room-id="${esc(id)}"`:`data-group-id="${esc(id)}"`} role="button" tabindex="0" aria-label="${esc(`${typeLabel} ${full}，${status}，${people.length} 位真实角色`)}"><title>${esc(`${typeLabel} · ${full}`)}</title><rect class="room-outline" x="${point.x}" y="${point.y}" width="${ROOM_WIDTH}" height="${height}" rx="8"></rect>${marker}${avatars}<text class="room-name" x="${point.x+14}" y="${point.y+height-27}">${esc(shown)}</text><text class="room-subline" x="${point.x+14}" y="${point.y+height-9}">${esc(subtitle)}</text></g>`;
    }).join('');
    const markup=`<g id="indoorMapContents"><g id="mapEdges">${edges}</g><g id="mapComponentLabels">${labels}</g><g id="mapRooms">${nodes}</g></g>`;
    if(svg.dataset.renderMarkup!==markup){svg.innerHTML=markup;svg.dataset.renderMarkup=markup;}
    svg.querySelectorAll('.room-avatar-image').forEach(image=>{if(image.dataset.fallbackReady)return;image.dataset.fallbackReady='true';image.addEventListener('error',()=>{image.setAttribute('visibility','hidden');image.nextElementSibling?.setAttribute('visibility','visible');},{once:true});});
    initializeMapFit();applyMapTransform();focusPendingNode();
  }
  function exitsForNode(node) {
    const inside=node.kind==='room'?new Set([node.id]):new Set(descendantRooms(node.id).map(room=>room.id)),rows=[];
    for(const edge of data().connections){const aInside=inside.has(edge.a),bInside=inside.has(edge.b);if(aInside===bInside)continue;const inner=roomById(aInside?edge.a:edge.b),outside=roomById(aInside?edge.b:edge.a);if(!inner || !outside)continue;if(node.kind==='room' && visibleNodeForRoom(outside.id))continue;rows.push({inside:inner,outside,edge});}
    return rows;
  }
  function renderRoomDetails() {
    const node=targetNode(),host=$('#indoorRoomDetails');$('#indoorSelectedTitle').textContent=node?.kind==='group'?'选中的房间组':'选中的房间';
    if(!node?.item){$('#indoorRoomCountHint').textContent=visibleNodes().length?'选择房间预览路线，或进入房间组查看下一层。':'在当前层新建房间或房间组。';const html='<p class="empty">请选择一个空间。</p>';if(host.dataset.renderMarkup!==html){host.innerHTML=html;host.dataset.renderMarkup=html;}return;}
    const item=node.item,people=nodeOccupants(node),exits=exitsForNode(node),path=node.kind==='room'?roomPathLabel(item):groupTitlePath(item.id).join(' / ') || item.name;
    $('#indoorRoomCountHint').textContent=`${node.kind==='group'?'房间组':'房间'} · ${path}`;
    const actions=node.kind==='group'?`<button type="button" class="secondary" data-group-action="enter" data-group-id="${esc(item.id)}">进入房间组</button>${item.entry_room_id?`<button type="button" class="secondary" data-group-action="route" data-group-id="${esc(item.id)}">预览路线</button>`:''}<button type="button" class="secondary" data-group-action="edit" data-group-id="${esc(item.id)}">编辑房间组</button>${item.id!==rootId() && childRooms(item.id).length===0 && childGroups(item.id).length===0?`<button type="button" class="quiet danger" data-group-action="delete" data-group-id="${esc(item.id)}">删除房间组</button>`:''}`:`<button type="button" class="secondary" data-room-action="edit" data-room-id="${esc(item.id)}">编辑房间</button><button type="button" class="quiet danger" data-room-action="delete" data-room-id="${esc(item.id)}">删除房间</button>`;
    const entry=node.kind==='group'?roomById(item.entry_room_id):null,children=node.kind==='group'?`<p>直接包含：${childRooms(item.id).length} 个房间、${childGroups(item.id).length} 个下级房间组</p><p>默认入口：${entry?esc(allRoomLabel(entry)):'未设置'}</p>`:'';
    const exitMarkup=exits.length?`<div class="indoor-exit-list"><h4>跨层出口</h4>${exits.map(row=>`<p><span>${esc(row.inside.name)} ↔ ${esc(allRoomLabel(row.outside))}</span><button type="button" class="quiet small" data-jump-room="${esc(row.outside.id)}">查看位置</button></p>`).join('')}</div>`:'';
    const html=`<h3>${esc(item.name || item.id)}</h3><p class="room-id">${node.kind==='group'?'房间组':'房间'} ID：${esc(item.id)}</p><p>${esc(item.description || '尚未填写描述。')}</p>${children}<p>${people.length?`已知角色 ${people.length} 位`:'暂无已知角色'}${node.kind==='room' && item.id===data().initial_room_id?' · 新角色的初始房间':''}</p>${exitMarkup}<div class="indoor-detail-actions">${actions}</div>`;
    if(host.dataset.renderMarkup!==html && !host.contains(document.activeElement)){host.innerHTML=html;host.dataset.renderMarkup=html;}
  }
  function renderRouteControls() {
    const status=$('#indoorRouteStatus'),pathLabel=$('#indoorRoutePath'),current=currentRoomId(),node=targetNode();let message='',error=false;
    if(contextSwitching)message='正在切换角色位置…';else if(!selectedActor)message='等待真实 QQ 会话；目前不能预览路线或移动角色。';else if(!current)message='当前角色尚未分配房间；房间与连接配置仍可管理。';else if(routeError){message=routeError;error=true;}else if(route?.pending)message='正在预览路线…';else if(node?.kind==='group' && !node.item?.entry_room_id)message='此房间组尚未设置入口，暂时不能预览路线或移动角色。';else if(route)message=route.message || (route.reachable?'可以到达此处。':'当前房间与所选位置不连通。');
    if(currentActivity())message=`${message?`${message} `:''}手动移动会中止当前活动。`;
    status.hidden=!message;status.textContent=message;status.classList.toggle('error',error || Boolean(route?.reachable===false));
    const routeAlreadyInGroup=route?.reachable && node?.kind==='group' && roomBelongsToGroup(current,node.id);
    pathLabel.textContent=routeAlreadyInGroup?'路线：已在此房间组，保持当前位置。':route?.reachable && route.path?.length?`路线：${route.path.map(roomTitle).join(' → ')}`:'';
    const alreadyInGroup=node?.kind==='group' && roomBelongsToGroup(current,node.id);
    $('#moveToRoomButton').textContent=moveBusy?'正在移动…':alreadyInGroup?'已在此房间组':node?.kind==='group'?`前往${nodeTitle('group',node.id)}入口`:'前往此房间';
    $('#moveToRoomButton').disabled=!selectedActor || contextSwitching || indoorWriteBusy || moveBusy || !route?.reachable || Boolean(route?.pending) || !node?.item || node.kind==='group' && (!node.item.entry_room_id || alreadyInGroup) || current===(node?.kind==='room'?node?.id:node?.item?.entry_room_id);
  }
  function endpointOptions(preferred,select) {
    const items=visibleNodes().filter(node=>node.kind==='room' || node.item.entry_room_id && roomById(node.item.entry_room_id));
    const markup=items.map(node=>`<option value="${esc(node.key)}">${esc(node.kind==='group'?`房间组 · ${node.item.name}（入口：${roomPathLabel(node.item.entry_room_id)}）`:node.item.name)}</option>`).join('');
    const old=select.value;if(select!==document.activeElement && select.dataset.optionMarkup!==markup){select.innerHTML=markup;select.dataset.optionMarkup=markup;}
    const wanted=preferred && items.some(node=>node.key===preferred)?preferred:items.some(node=>node.key===old)?old:items[0]?.key || '';if(wanted)select.value=wanted;
    return items;
  }
  function renderedEndpointLabel(roomId,visibleNode) {
    const room=roomById(roomId);if(!room)return roomId;
    const group=visibleNode?.kind==='group'?groupById(visibleNode.id):null;
    return group?`${group.name}（入口 ${room.name}）`:room.name;
  }
  function renderConnections() {
    const from=$('#connectionFrom'),to=$('#connectionTo'),items=endpointOptions(connectionDraft.a,from);endpointOptions(connectionDraft.b,to);
    connectionDraft={a:from.value,b:to.value};const edges=projectedEdges(),edgeRows=[];
    for(const projection of edges)for(const edge of projection.items){edgeRows.push({edge,a:renderedEndpointLabel(edge.a,visibleNodeForRoom(edge.a)),b:renderedEndpointLabel(edge.b,visibleNodeForRoom(edge.b))});}
    const host=$('#indoorConnectionList'),html=!edgeRows.length?'<li class="indoor-connection-row"><span>当前层没有可见连接。</span></li>':edgeRows.map(({edge,a,b})=>`<li class="indoor-connection-row"><span>${esc(`${roomTitle(edge.a)} ↔ ${roomTitle(edge.b)}`)} <small>· ${esc(a)} / ${esc(b)}</small></span><button type="button" class="quiet danger indoor-connection-remove" data-remove-edge="${esc(edgeKey(edge.a,edge.b))}" data-edge-a="${esc(edge.a)}" data-edge-b="${esc(edge.b)}" aria-label="移除 ${esc(roomTitle(edge.a))} 与 ${esc(roomTitle(edge.b))} 的连接">移除</button></li>`).join('');
    if(host.dataset.renderMarkup!==html && !host.contains(document.activeElement)){host.innerHTML=html;host.dataset.renderMarkup=html;}
    const enabledEndpoints=items.filter(item=>item.kind==='room' || item.item.entry_room_id).length;
    $('#connectionFrom').disabled=indoorWriteBusy || enabledEndpoints<2;$('#connectionTo').disabled=indoorWriteBusy || enabledEndpoints<2;$('#addConnectionButton').disabled=indoorWriteBusy || enabledEndpoints<2;
  }
  function renderCurrent() {
    const room=roomById(currentRoomId());
    if(!selectedActor){const life=occupants().filter(person=>person.life && person.room_id);if(life.length){$('#indoorCurrentRoom').textContent=life.map(person=>`${person.name || `QQ ${person.qq || ''}`}（QQ ${person.qq || '—'}）· ${roomPathLabel(person.room_id)}`).join('、');return;}}
    $('#indoorCurrentRoom').textContent=room?roomPathLabel(room):'尚未分配房间';
  }
  function renderCount() {$('#indoorRoomCount').textContent=`${data().rooms.length} 个房间 · ${Math.max(0,data().groups.length-1)} 个房间组`;}
  function renderEditorOptions() {
    const roomParent=$('#indoorRoomParent'),roomParents=sortNamed(data().groups),roomParentMarkup=roomParents.map(group=>`<option value="${esc(group.id)}">${esc(groupTitlePath(group.id).join(' / ') || group.name)}</option>`).join('');
    if(roomParent!==document.activeElement && roomParent.dataset.optionMarkup!==roomParentMarkup){roomParent.innerHTML=roomParentMarkup;roomParent.dataset.optionMarkup=roomParentMarkup;}
    if(roomDraft.group_id && roomParents.some(group=>String(group.id)===String(roomDraft.group_id)))roomParent.value=roomDraft.group_id;else if(currentGroupId && roomParents.some(group=>String(group.id)===String(currentGroupId)))roomParent.value=currentGroupId;
    const editing=groupById(editingGroupId),excluded=editing?descendantGroupIds(editing.id):new Set(),parentOptions=data().groups.filter(group=>!excluded.has(String(group.id)) && String(group.id)!==String(editing?.id || ''));
    const parentMarkup=`<option value="">无上级</option>${sortNamed(parentOptions).map(group=>`<option value="${esc(group.id)}">${esc(groupTitlePath(group.id).join(' / ') || group.name)}</option>`).join('')}`;
    const parentSelect=$('#indoorGroupParent');if(parentSelect!==document.activeElement && parentSelect.dataset.optionMarkup!==parentMarkup){parentSelect.innerHTML=parentMarkup;parentSelect.dataset.optionMarkup=parentMarkup;}
    if(groupDraft.parent_id && parentOptions.some(group=>String(group.id)===String(groupDraft.parent_id)))parentSelect.value=groupDraft.parent_id;else if(!editingGroupId && currentGroupId)parentSelect.value=currentGroupId;else parentSelect.value='';
    const groupId=editingGroupId || groupDraft.id || '__new_group__';
    let allowed=[];
    if(groupId==='__new_group__')allowed=[];else if(editingGroupId)allowed=descendantRooms(groupId);else allowed=[];
    const selectedParent=groupDraft.parent_id || parentSelect.value;
    if(!editingGroupId && selectedParent)allowed=descendantRooms(selectedParent);
    const entryMarkup=`<option value="">${allowed.length?'未设置':'当前没有可用房间'}</option>${sortNamed(allowed).map(room=>`<option value="${esc(room.id)}">${esc(allRoomLabel(room))}</option>`).join('')}`;
    const entrySelect=$('#indoorGroupEntry');if(entrySelect!==document.activeElement && entrySelect.dataset.optionMarkup!==entryMarkup){entrySelect.innerHTML=entryMarkup;entrySelect.dataset.optionMarkup=entryMarkup;}
    if(groupDraft.entry_room_id && allowed.some(room=>String(room.id)===String(groupDraft.entry_room_id)))entrySelect.value=groupDraft.entry_room_id;else entrySelect.value='';
    const isRoot=Boolean(editingGroupId && editingGroupId===rootId());parentSelect.disabled=indoorWriteBusy || isRoot;$('#indoorGroupId').disabled=indoorWriteBusy || Boolean(editingGroupId);
  }
  function render() {
    renderContexts();const idSet=new Set(data().groups.map(group=>String(group.id)));if(!idSet.has(String(currentGroupId)))currentGroupId=rootId();
    const visible=visibleNodes();if(selectedNodeKey && !visible.some(node=>node.key===selectedNodeKey))selectedNodeKey='';
    if(!selectedNodeKey){const current=visibleNodeForRoom(currentRoomId());selectedNodeKey=current?.key || visible[0]?.key || '';}
    renderBreadcrumbs();renderCount();renderCurrent();renderInitialOptions();renderEditorOptions();renderMap();renderRoomDetails();renderConnections();renderNotice();renderRouteControls();syncWriteControls();
  }
  function syncWriteControls() {
    const busy=indoorWriteBusy || moveBusy,rooms=data().rooms,empty=rooms.length===0;
    $('#saveInitialRoomButton').disabled=busy || empty;$('#indoorInitialRoom').disabled=busy || empty;$('#saveIndoorRoomButton').disabled=busy;$('#saveIndoorGroupButton').disabled=busy;
    for(const selector of ['#indoorRoomName','#indoorRoomParent','#indoorRoomDescription','#indoorGroupName','#indoorGroupDescription','#indoorGroupParent','#indoorGroupEntry'])$(selector).disabled=busy || selector==='#indoorGroupParent' && editingGroupId===rootId();
    $('#indoorRoomId').disabled=busy || Boolean(editingRoomId);$('#indoorGroupId').disabled=busy || Boolean(editingGroupId);
    const firstRoom=!editingRoomId && empty;$('#indoorSetInitial').disabled=busy || firstRoom || Boolean(editingRoomId && data().initial_room_id===editingRoomId);
    $$('.indoor-connection-remove').forEach(button=>{button.disabled=busy;});$('#indoorContextSelect').disabled=contextSwitching || busy || availableActors().length<2;
    renderRouteControls();
  }
  function acceptSnapshot(snapshot) {
    $('#indoorLoadError').hidden=true;const actors=availableActors();if(selectedActor)selectedActor=actors.find(item=>actorKey(item)===actorKey(selectedActor)) || null;
    if(!selectedActor && actors.length){const requested=normalizeContext(snapshot?.context) || normalizeContext(getActor()),account=String(snapshot?.selected_account_id || '');selectedActor=actors.find(item=>item.account_id===account) || actors.find(item=>requested && item.context?.character===requested.character) || actors.find(item=>item.life) || actors[0];}
    if(selectedActor?.life && selectedActor.account_id && String(snapshot?.selected_account_id || '')!==selectedActor.account_id && !initialLifeSelectionRequested){initialLifeSelectionRequested=true;contextSwitching=true;render();Promise.resolve(onContextChange(actorSelection(selectedActor))).catch(error=>{contextSwitching=false;routeError=error.message || '生活账号位置未能加载，请重试。';render();toast(routeError,true);});return;}
    const nextGraph=graphSignature(),currentActor=actorKey(selectedActor),roomId=currentRoomId();if(graphKey && graphKey!==nextGraph){invalidateRoute();mapScale=1;}
    if(dataActorKey && dataActorKey!==currentActor){invalidateRoute();setSelectedForCurrent();}else if(!dataActorKey && roomId)setSelectedForCurrent();
    if(route && route.from_room_id!==roomId)invalidateRoute();graphKey=nextGraph;dataActorKey=currentActor;contextSwitching=false;
    if(!initialDirty || initialDraft && !roomById(initialDraft)){initialDraft=data().initial_room_id || '';initialDirty=false;}
    if(selectedNodeKey && !visibleNodes().some(node=>node.key===selectedNodeKey))selectedNodeKey='';routeError='';render();
  }
  function roomConfig(overrides={}) {
    const current=data();return {schema_version:2,root_group_id:current.root_group_id,groups:(overrides.groups || current.groups).map(group=>({...group})),rooms:(overrides.rooms || current.rooms).map(room=>({...room})),connections:(overrides.connections || current.connections).map(edge=>({a:edge.a,b:edge.b})),initial_room_id:overrides.initial_room_id!==undefined?overrides.initial_room_id:current.initial_room_id || ''};
  }
  function validGroupEntries(config) {
    const groups=config.groups.map(group=>({...group})),byId=new Map(groups.map(group=>[String(group.id),group]));
    const contains=(roomId,groupId)=>{const room=config.rooms.find(item=>String(item.id)===String(roomId));if(!room)return false;let cursor=String(room.group_id || ''),seen=new Set();while(cursor && !seen.has(cursor)){if(cursor===String(groupId))return true;seen.add(cursor);cursor=String(byId.get(cursor)?.parent_id || '');}return false;};
    return groups.map(group=>{if(group.entry_room_id && !contains(group.entry_room_id,group.id)){const entry=sortNamed(config.rooms).find(room=>contains(room.id,group.id));return {...group,entry_room_id:entry?.id || ''};}return group;});
  }
  async function withWriteGate(button,action) {if(indoorWriteBusy || moveBusy || contextSwitching){toast('已有环境操作正在进行，请稍后重试。',true);return false;}indoorWriteBusy=true;syncWriteControls();if(button)button.disabled=true;try{return await run(null,action);}finally{indoorWriteBusy=false;syncWriteControls();}}
  async function saveConfiguration(indoor,relocate={}) {indoor={...indoor,groups:validGroupEntries(indoor)};const result=await api('indoor/save',{indoor,relocate});accept(result);render();return result;}
  function nonce() {if(globalThis.crypto?.randomUUID)return globalThis.crypto.randomUUID();if(globalThis.crypto?.getRandomValues){const bytes=new Uint8Array(16);globalThis.crypto.getRandomValues(bytes);return [...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');}return `${Date.now()}_${Math.random().toString(36).slice(2)}`;}
  const generatedId=kind=>`${kind}_${nonce().replaceAll('-','').toLowerCase()}`;
  function setEditorHeader(kind,item=null) {const title=item?`编辑${kind==='group'?'房间组':'房间'} · ${item.name}`:`新建${kind==='group'?'房间组':'房间'}`;$('#indoorEditorTitle').textContent=title;$('#indoorEditorHint').textContent=kind==='group'?'设置上级房间组与默认入口；可将其移动到其他上级。':'选择所属房间组；房间位置与关系由连接配置决定。';}
  function showRoomEditor(room=null) {
    editingRoomId=room?.id || '';editingGroupId='';editingKind='room';roomDraft=room?{id:room.id,name:room.name || '',description:room.description || '',group_id:room.group_id || rootId(),initial:room.id===data().initial_room_id}:{id:'',name:'',description:'',group_id:currentGroupId || rootId(),initial:data().rooms.length===0};
    roomForm.reset();$('#indoorRoomId').value=roomDraft.id;$('#indoorRoomName').value=roomDraft.name;$('#indoorRoomDescription').value=roomDraft.description;$('#indoorSetInitial').checked=roomDraft.initial;$('#indoorRoomFormError').hidden=true;
    setEditorKind('room');$('#indoorCreateKind').disabled=Boolean(room);setEditorHeader('room',room);roomFormInitialized=true;groupFormInitialized=true;editorDirty=false;renderEditorOptions();selectTab('create');
  }
  function showGroupEditor(group=null) {
    editingGroupId=group?.id || '';editingRoomId='';editingKind='group';groupDraft=group?{id:group.id,name:group.name || '',description:group.description || '',parent_id:group.parent_id || '',entry_room_id:group.entry_room_id || ''}:{id:'',name:'',description:'',parent_id:currentGroupId || rootId(),entry_room_id:''};
    groupForm.reset();$('#indoorGroupId').value=groupDraft.id;$('#indoorGroupName').value=groupDraft.name;$('#indoorGroupDescription').value=groupDraft.description;$('#indoorGroupFormError').hidden=true;
    setEditorKind('group');$('#indoorCreateKind').disabled=Boolean(group);setEditorHeader('group',group);roomFormInitialized=true;groupFormInitialized=true;editorDirty=false;renderEditorOptions();$('#indoorGroupEntry').value=groupDraft.entry_room_id;selectTab('create');
  }
  function collectRoomDraft() {roomDraft={id:$('#indoorRoomId').value.trim(),name:$('#indoorRoomName').value,description:$('#indoorRoomDescription').value,group_id:$('#indoorRoomParent').value,initial:$('#indoorSetInitial').checked};roomFormInitialized=true;editorDirty=true;}
  function collectGroupDraft() {groupDraft={id:$('#indoorGroupId').value.trim(),name:$('#indoorGroupName').value,description:$('#indoorGroupDescription').value,parent_id:$('#indoorGroupParent').value,entry_room_id:$('#indoorGroupEntry').value};groupFormInitialized=true;editorDirty=true;}
  function rememberInitialDraft() {initialDraft=$('#indoorInitialRoom').value;initialDirty=initialDraft!==(data().initial_room_id || '');}
  function rememberConnectionDraft() {connectionDraft={a:$('#connectionFrom').value,b:$('#connectionTo').value};}
  async function submitRoom(event) {
    event.preventDefault();collectRoomDraft();$('#indoorRoomFormError').hidden=true;if(!$('#indoorRoomName').reportValidity())return;
    const current=roomConfig(),rooms=[...current.rooms],id=editingRoomId || roomDraft.id || generatedId('room'),next={id,name:roomDraft.name.trim(),description:roomDraft.description.trim(),group_id:roomDraft.group_id || rootId()};
    if(!next.name){$('#indoorRoomFormError').textContent='请填写房间名称。';$('#indoorRoomFormError').hidden=false;return;}if(!groupById(next.group_id)){showRoomError('请选择有效的所属房间组。');return;}
    if(editingRoomId){const index=rooms.findIndex(room=>room.id===editingRoomId);if(index<0){showRoomError('此房间已不存在，请同步后重试。');return;}rooms[index]=next;}
    else if(rooms.some(room=>room.id===id)){showRoomError('此房间 ID 已存在，请换一个 ID。');return;}else rooms.push(next);
    let initial=current.initial_room_id;if(roomDraft.initial)initial=id;else if(!initial && !editingRoomId)initial=id;
    const savedConfig=roomConfig({rooms,initial_room_id:initial}),wasEditing=Boolean(editingRoomId);
    try{const saved=await withWriteGate($('#saveIndoorRoomButton'),()=>saveConfiguration(savedConfig));if(saved===false)return;editingRoomId='';roomDraft={id:'',name:'',description:'',group_id:'',initial:false};roomForm.reset();groupForm.reset();roomFormInitialized=false;groupFormInitialized=false;editorDirty=false;$('#indoorCreateKind').disabled=false;selectedNodeKey=nodeKey('room',id);notice={message:wasEditing?'房间已更新。':'房间已保存。',error:false};selectTab('view');render();toast('房间已保存');}
    catch(error){showRoomError(error.message || '房间未能保存，请重试。');}
  }
  function showRoomError(message) {$('#indoorRoomFormError').textContent=message;$('#indoorRoomFormError').hidden=false;}
  async function submitGroup(event) {
    event.preventDefault();collectGroupDraft();$('#indoorGroupFormError').hidden=true;if(!$('#indoorGroupName').reportValidity())return;
    const current=roomConfig(),groups=[...current.groups],id=editingGroupId || groupDraft.id || generatedId('group'),parent=groupDraft.parent_id || '',next={id,name:groupDraft.name.trim(),description:groupDraft.description.trim(),parent_id:editingGroupId===rootId()?'':parent,entry_room_id:groupDraft.entry_room_id || ''};
    const descendants=editingGroupId?descendantGroupIds(editingGroupId):new Set();
    if(!next.name){showGroupError('请填写房间组名称。');return;}if(editingGroupId!==rootId() && (!groupById(parent) || descendants.has(String(parent)) || String(parent)===String(id))){showGroupError('上级房间组无效，或会形成循环层级。');return;}
    const validEntries=editingGroupId?descendantRooms(id):descendantRooms(parent);if(next.entry_room_id && !validEntries.some(room=>String(room.id)===String(next.entry_room_id))){showGroupError('默认入口必须是此房间组内或下级房间组中的房间。');return;}
    if(editingGroupId){const index=groups.findIndex(group=>group.id===editingGroupId);if(index<0){showGroupError('此房间组已不存在，请同步后重试。');return;}groups[index]=next;}else if(groups.some(group=>group.id===id)){showGroupError('此房间组 ID 已存在，请换一个 ID。');return;}else groups.push(next);
    try{const saved=await withWriteGate($('#saveIndoorGroupButton'),()=>saveConfiguration(roomConfig({groups})));if(saved===false)return;editingGroupId='';groupDraft={id:'',name:'',description:'',parent_id:'',entry_room_id:''};roomForm.reset();groupForm.reset();roomFormInitialized=false;groupFormInitialized=false;editorDirty=false;$('#indoorCreateKind').disabled=false;selectedNodeKey=nodeKey('group',id);notice={message:'房间组已保存。',error:false};selectTab('view');render();toast('房间组已保存');}
    catch(error){showGroupError(error.message || '房间组未能保存，请重试。');}
  }
  function showGroupError(message) {$('#indoorGroupFormError').textContent=message;$('#indoorGroupFormError').hidden=false;}
  function clearEditor() {editingRoomId='';editingGroupId='';editingKind='room';roomDraft={id:'',name:'',description:'',group_id:currentGroupId || rootId(),initial:data().rooms.length===0};groupDraft={id:'',name:'',description:'',parent_id:currentGroupId || rootId(),entry_room_id:''};roomForm.reset();groupForm.reset();roomFormInitialized=false;groupFormInitialized=false;editorDirty=false;$('#indoorRoomFormError').hidden=true;$('#indoorGroupFormError').hidden=true;$('#indoorCreateKind').disabled=false;setEditorKind('room');setEditorHeader('room');renderEditorOptions();selectTab('view');}
  function roomEditorAction(room) {showRoomEditor(room);$('#indoorRoomName').focus();}
  function groupEditorAction(group) {showGroupEditor(group);$('#indoorGroupName').focus();}
  function confirmDeleteRoom(room) {
    const config=data(),remaining=sortNamed(config.rooms.filter(item=>item.id!==room.id)),occupied=occupantsForRoom(room.id).length,wasInitial=room.id===config.initial_room_id,needsTarget=remaining.length>0 && (occupied>0 || wasInitial);
    let copy='';if(!remaining.length)copy='<p>这是最后一个房间。删除后，角色在此房间的位置会被清空，并取消新角色的初始房间。</p>';else if(occupied && wasInitial)copy=`<p>目前有 ${occupied} 位已知角色位于此房间。删除后需迁移这些角色，并为新角色选择新的初始房间。</p>`;else if(occupied)copy=`<p>目前有 ${occupied} 位已知角色位于此房间。删除后需选择迁移目标。</p>`;else if(wasInitial)copy='<p>此房间是新角色的初始房间。删除后请选择新的初始房间。</p>';else copy='<p>目前没有已知角色位于此房间。删除后不需要迁移角色。</p>';
    const options=remaining.map(item=>`<option value="${esc(item.id)}" ${item.id===config.initial_room_id?'selected':''}>${esc(allRoomLabel(item))}</option>`).join(''),targetMarkup=needsTarget?`<label>${occupied?'迁移目标':'新的初始房间'}<select name="relocateTarget" required>${options}</select></label>`:'';
    modal('删除房间',`${copy}${targetMarkup}`,async body=>{const target=needsTarget?$('select[name="relocateTarget"]',body).value:'',rooms=config.rooms.filter(item=>item.id!==room.id),connections=config.connections.filter(edge=>edge.a!==room.id && edge.b!==room.id),groups=config.groups.map(group=>group.entry_room_id===room.id?{...group,entry_room_id:descendantRooms(group.id).find(item=>item.id!==room.id)?.id || ''}:group),initial=wasInitial?(remaining.length?target:''):config.initial_room_id,relocate=occupied || !remaining.length?{[room.id]:remaining.length?target:''}:{};
      const saved=await withWriteGate(null,()=>saveConfiguration(roomConfig({rooms,groups,connections,initial_room_id:initial}),relocate));if(saved===false)throw Error('已有环境配置正在保存，请稍后重试。');selectedNodeKey=target?nodeKey('room',target):'';notice={message:'房间已删除。',error:false};render();toast('房间已删除');},'删除房间');
  }
  function confirmDeleteGroup(group) {if(group.id===rootId() || childRooms(group.id).length || childGroups(group.id).length)return;modal('删除房间组',`<p>确定删除空房间组“${esc(group.name)}”吗？</p>`,async()=>{const groups=data().groups.filter(item=>item.id!==group.id);const saved=await withWriteGate(null,()=>saveConfiguration(roomConfig({groups})));if(saved===false)throw Error('已有环境配置正在保存，请稍后重试。');selectedNodeKey='';notice={message:'空房间组已删除。',error:false};render();toast('房间组已删除');},'删除房间组');}
  async function saveInitialRoom(event) {event.preventDefault();rememberInitialDraft();const selected=$('#indoorInitialRoom').value;if(!selected || !roomById(selected)){notice={message:'请选择一个有效的初始房间。',error:true};renderNotice();return;}if(!initialDirty){notice={message:'该房间已经是新角色的初始房间。',error:false};renderNotice();return;}notice=null;renderNotice();try{const saved=await withWriteGate($('#saveInitialRoomButton'),()=>saveConfiguration(roomConfig({initial_room_id:selected})));if(saved===false)return;toast('初始房间已保存');}catch(error){notice={message:error.message || '初始房间未能保存。',error:true};render();}}
  function endpointRoom(endpoint) {const parsed=parseNodeKey(endpoint);if(parsed?.kind==='room')return roomById(parsed.id);if(parsed?.kind==='group'){const group=groupById(parsed.id);return group?.entry_room_id?roomById(group.entry_room_id):null;}return null;}
  async function submitConnection(event) {event.preventDefault();rememberConnectionDraft();$('#indoorConnectionError').hidden=true;const first=endpointRoom(connectionDraft.a),second=endpointRoom(connectionDraft.b);if(!first || !second){$('#indoorConnectionError').textContent='请选择有有效入口的两个空间。';$('#indoorConnectionError').hidden=false;return;}if(first.id===second.id){$('#indoorConnectionError').textContent='两个空间的入口不能是同一个房间。';$('#indoorConnectionError').hidden=false;return;}if(data().connections.some(edge=>edgeKey(edge.a,edge.b)===edgeKey(first.id,second.id))){$('#indoorConnectionError').textContent='这两个入口房间已经连接。';$('#indoorConnectionError').hidden=false;return;}
    const connections=[...data().connections,{a:edgeEnds(first.id,second.id)[0],b:edgeEnds(first.id,second.id)[1]}];try{const saved=await withWriteGate($('#addConnectionButton'),()=>saveConfiguration(roomConfig({connections})));if(saved===false)return;notice={message:`${nodeTitle(endpointRoom(connectionDraft.a)?.id===first.id && connectionDraft.a.startsWith('group:')?'group':'room',connectionDraft.a.split(/:(.*)/s).at(-1))} 与 ${nodeTitle(connectionDraft.b.startsWith('group:')?'group':'room',connectionDraft.b.split(/:(.*)/s).at(-1))} 的入口房间已连接。`,error:false};render();toast('连接已添加');}catch(error){$('#indoorConnectionError').textContent=error.message || '连接未能保存。';$('#indoorConnectionError').hidden=false;}}
  async function removeConnection(button) {if(indoorWriteBusy || moveBusy)return;const a=button.dataset.edgeA,b=button.dataset.edgeB;if(!data().connections.some(edge=>edgeKey(edge.a,edge.b)===edgeKey(a,b)))return;try{const saved=await withWriteGate(button,()=>saveConfiguration(roomConfig({connections:data().connections.filter(edge=>edgeKey(edge.a,edge.b)!==edgeKey(a,b))})));if(saved===false)return;notice={message:`${roomTitle(a)} 与 ${roomTitle(b)} 的连接已移除。`,error:false};render();toast('连接已移除');}catch(error){$('#indoorConnectionError').textContent=error.message || '连接未能移除。';$('#indoorConnectionError').hidden=false;}}
  let groupNavigationAnimation=null;
  function animateGroupNavigation(direction) {
    const target=$('#indoorViewPanel');if(!target?.animate)return;
    const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches || false;
    try {groupNavigationAnimation?.cancel();groupNavigationAnimation=target.animate(reduced?[{opacity:.82},{opacity:1}]:[{opacity:.78,transform:`translateX(${direction*10}px)`},{opacity:1,transform:'translateX(0)'}],{duration:reduced?90:170,easing:'cubic-bezier(0.16, 1, 0.3, 1)'});groupNavigationAnimation.finished.catch(()=>{});}catch{groupNavigationAnimation=null;}
  }
  function navigateGroup(id,focus=true) {if(!groupById(id))return;const previousGroup=currentGroupId,previousDepth=groupAncestors(previousGroup).length,nextDepth=groupAncestors(id).length,direction=nextDepth>=previousDepth?1:-1;currentGroupId=String(id);invalidateRoute();selectedNodeKey='';const current=visibleNodeForRoom(currentRoomId(),currentGroupId);if(current){selectedNodeKey=current.key;if(focus)pendingFocusNodeKey=current.key;}mapFit=null;mapScale=1;render();if(String(previousGroup)!==String(id))animateGroupNavigation(direction);}
  function locateRoom(roomId) {const room=roomById(roomId);if(!room)return;const owner=groupById(room.group_id);navigateGroup(owner?.id || rootId(),false);selectedNodeKey=nodeKey('room',room.id);pendingFocusNodeKey=selectedNodeKey;render();}
  function requestRoute(targetId,kind='room',focus=false) {
    if(contextSwitching || indoorWriteBusy || moveBusy)return;const node=kind==='group'?groupById(targetId):roomById(targetId);if(!node)return;
    if(kind==='group' && !node.entry_room_id){selectedNodeKey=nodeKey(kind,targetId);route=null;routeError='此房间组尚未设置默认入口，暂时无法预览路线。';render();return;}
    selectedNodeKey=nodeKey(kind,targetId);routeError='';notice=null;if(!selectedActor){invalidateRoute();routeError='等待真实 QQ 会话首次连接后，才能预览路线。';render();return;}
    const sequence=++routeSequence,requestedActor=actorKey(selectedActor),requestedGraph=graphSignature();route={pending:true,reachable:false,path:[],from_room_id:currentRoomId(),to_room_id:targetId,target_kind:kind};if(focus)pendingFocusNodeKey=selectedNodeKey;render();
    api('indoor/route',{room_id:targetId,...actorRouteSelection(selectedActor)},null).then(result=>{if(sequence!==routeSequence || selectedNodeKey!==nodeKey(kind,targetId) || requestedActor!==actorKey(selectedActor) || requestedGraph!==graphSignature())return;route={...result,pending:false,path:Array.isArray(result.path)?result.path:[],target_kind:kind};routeError='';render();}).catch(error=>{if(sequence!==routeSequence || selectedNodeKey!==nodeKey(kind,targetId) || requestedActor!==actorKey(selectedActor) || requestedGraph!==graphSignature())return;route={...route,pending:false};routeError=error.message || '路线预览失败，请重试。';render();});
  }
  async function moveToSelected() {const node=targetNode();if(!selectedActor || contextSwitching || indoorWriteBusy || moveBusy || !route?.reachable || route.pending || !node || node.kind==='group' && roomBelongsToGroup(currentRoomId(),node.id))return;const target=node.id,kind=node.kind,actor=actorKey(selectedActor),snapshot=route;moveBusy=true;renderRouteControls();syncWriteControls();
    try{const result=await api('indoor/move',{room_id:target,token:nonce(),...actorRouteSelection(selectedActor)},null);if(actor!==actorKey(selectedActor))return;if(result.move?.ok===false){route=snapshot;routeError=result.move.message || '移动未能完成。';render();return;}accept(result);notice={message:result.move?.message || `已前往${kind==='group'?`${nodeTitle('group',target)}入口`:nodeTitle('room',target)}。`,error:false};selectedNodeKey=nodeKey(kind,target);render();toast('角色位置已更新');}
    catch(error){if(actor===actorKey(selectedActor)){route=snapshot;routeError=error.message || '移动未能完成，请重试。';render();}}finally{moveBusy=false;syncWriteControls();renderRouteControls();}}
  function fitMap() {mapFit=true;mapScale=1;$('#indoorMapViewport').scrollTo({left:0,top:0,behavior:'smooth'});applyMapTransform();}

  for(const [index,tab] of tabs.entries())tab.onclick=()=>selectTab(index===0?'view':'create');
  $('#indoorTabs').onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const active=tabs.findIndex(tab=>tab.getAttribute('aria-selected')==='true'),next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(active+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;selectTab(next===0?'view':'create',true);};
  $('#indoorContextSelect').onchange=()=>switchContext(JSON.parse($('#indoorContextSelect').value));$('#indoorInitialRoom').onchange=rememberInitialDraft;initialForm.onsubmit=saveInitialRoom;connectionForm.addEventListener('change',rememberConnectionDraft);connectionForm.onsubmit=submitConnection;
  roomForm.addEventListener('input',collectRoomDraft);roomForm.addEventListener('change',collectRoomDraft);roomForm.onsubmit=submitRoom;groupForm.addEventListener('input',collectGroupDraft);groupForm.addEventListener('change',()=>{collectGroupDraft();renderEditorOptions();});groupForm.onsubmit=submitGroup;
  $('#indoorCreateKind').onchange=()=>{if(editingRoomId || editingGroupId)return;setEditorKind($('#indoorCreateKind').value);setEditorHeader($('#indoorCreateKind').value);};
  $('#indoorGroupParent').addEventListener('change',()=>{collectGroupDraft();renderEditorOptions();});
  $('#cancelIndoorEdit').onclick=$('#cancelIndoorEditFooter').onclick=()=>clearEditor();groupForm.addEventListener('click',event=>{if(event.target.closest('[data-cancel-indoor-edit]'))clearEditor();});
  $('#firstRoomButton').onclick=()=>{selectTab('create');$('#indoorCreateKind').value='room';setEditorKind('room');$('#indoorRoomName').focus();};
  $('#retryIndoorButton').onclick=()=>run($('#retryIndoorButton'),async()=>{try{await refresh();}catch(error){$('#indoorLoadError').hidden=false;$('span',$('#indoorLoadError')).textContent=`环境未能加载：${error.message || '请检查连接后重试。'}`;throw error;}}).catch(()=>{});
  $('#indoorViewPanel').addEventListener('click',event=>{
    const mapNode=event.target.closest('.map-node');if(mapNode){requestRoute(mapNode.dataset.nodeId,mapNode.dataset.nodeKind);return;}
    const roomAction=event.target.closest('[data-room-action]');if(roomAction){const room=roomById(roomAction.dataset.roomId);if(room && roomAction.dataset.roomAction==='edit')showRoomEditor(room);else if(room)confirmDeleteRoom(room);return;}
    const groupAction=event.target.closest('[data-group-action]');if(groupAction){const group=groupById(groupAction.dataset.groupId);if(!group)return;if(groupAction.dataset.groupAction==='enter')navigateGroup(group.id);else if(groupAction.dataset.groupAction==='route')requestRoute(group.id,'group');else if(groupAction.dataset.groupAction==='edit')showGroupEditor(group);else if(groupAction.dataset.groupAction==='delete')confirmDeleteGroup(group);return;}
    const breadcrumb=event.target.closest('[data-go-group]');if(breadcrumb){navigateGroup(breadcrumb.dataset.goGroup);return;}
    const jump=event.target.closest('[data-jump-room]');if(jump){locateRoom(jump.dataset.jumpRoom);return;}
    const remove=event.target.closest('[data-remove-edge]');if(remove)removeConnection(remove);
  });
  $('#indoorMap').addEventListener('keydown',event=>{if(!['Enter',' '].includes(event.key))return;const node=event.target.closest('.map-node');if(!node)return;event.preventDefault();requestRoute(node.dataset.nodeId,node.dataset.nodeKind,true);});
  $('#indoorZoomIn').onclick=()=>{mapFit=false;mapScale=Math.min(2.4,mapScale*1.2);applyMapTransform();};$('#indoorZoomOut').onclick=()=>{mapFit=false;mapScale=Math.max(.65,mapScale/1.2);applyMapTransform();};$('#indoorMapFit').onclick=fitMap;$('#locateCurrentButton').onclick=()=>locateRoom(currentRoomId());
  $('#indoorMap').addEventListener('pointerdown',event=>{if(mapFit || event.target.closest('.map-node') || event.button!==0)return;const viewport=$('#indoorMapViewport');pointerDrag={x:event.clientX,y:event.clientY,scrollLeft:viewport.scrollLeft,scrollTop:viewport.scrollTop};event.currentTarget.setPointerCapture(event.pointerId);});
  $('#indoorMap').addEventListener('pointermove',event=>{if(!pointerDrag)return;const viewport=$('#indoorMapViewport');viewport.scrollLeft=pointerDrag.scrollLeft-(event.clientX-pointerDrag.x);viewport.scrollTop=pointerDrag.scrollTop-(event.clientY-pointerDrag.y);});
  for(const name of ['pointerup','pointercancel','lostpointercapture'])$('#indoorMap').addEventListener(name,()=>{pointerDrag=null;});$('#moveToRoomButton').onclick=moveToSelected;window.addEventListener('resize',()=>{if(mapFit)applyMapTransform();});
  const initialGroup=data().root_group_id;currentGroupId=initialGroup;renderContexts();render();
  return {accept:acceptSnapshot,renderContexts,getActor:()=>selectedActor?.context || null,loadFailed(error){const node=$('#indoorLoadError');$('span',node).textContent=`环境未能加载：${error?.message || '请检查连接后重试。'}`;node.hidden=false;contextSwitching=false;invalidateRoute();routeError=error?.message || '环境未能加载。';renderRouteControls();syncWriteControls();},onMainTab(){initializeMapFit();applyMapTransform();focusPendingNode();}};
}
