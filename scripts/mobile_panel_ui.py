# -*- coding: utf-8 -*-
CSS = r"""
#kuBox{font:14px system-ui,-apple-system,"Segoe UI",sans-serif;color:#e8e9f2;background:#101116;line-height:1.45}
#kuBox *{box-sizing:border-box}
#kuBox button,#kuBox input,#kuBox select{font:inherit}
#kuBox button{cursor:pointer;min-height:44px;border-radius:12px;border:1px solid #343848;background:#202532;color:#edf1ff;padding:9px 12px}
#kuBox button:disabled{opacity:.4;cursor:not-allowed}
#kuBox button:focus-visible,#kuBox input:focus-visible,#kuBox select:focus-visible{outline:2px solid #82aaff;outline-offset:2px}
#kuBox .ku-top{display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:2;background:#101116;padding:5px 0 10px}
#kuBox h1{font-size:18px;margin:0;flex:1}#kuBox h2{font-size:15px;margin:0 0 9px}
#kuBox .ku-tabs,#kuBox .ku-period{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding:4px 0 12px}
#kuBox .ku-tabs button,#kuBox .ku-period button{white-space:nowrap;flex:1;min-width:66px}
#kuBox .ku-tabs button[aria-selected=true],#kuBox .ku-period button[aria-selected=true]{background:#426ac2;border-color:#759cf5}
#kuBox .ku-pane[hidden],#kuBox .ku-editor[hidden]{display:none}
#kuBox .ku-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
@media(min-width:650px){#kuBox .ku-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
#kuBox .ku-card{border:1px solid #2e3442;background:#1a1e28;border-radius:15px;padding:14px;margin:0 0 10px;min-width:0}
#kuBox .ku-grid .ku-card{margin:0}#kuBox .ku-big{font-size:19px;font-weight:700;overflow-wrap:anywhere}
#kuBox .ku-muted{color:#aab4c8;font-size:12px}#kuBox .ku-row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;border-top:1px solid #2e3442;padding:8px 0}
#kuBox .ku-row:first-child{border:0}#kuBox .ku-row span:last-child{text-align:right;overflow-wrap:anywhere}
#kuBox .ku-bar{height:8px;border-radius:5px;background:#303849;overflow:hidden;margin:6px 0 12px}
#kuBox .ku-bar>i{display:block;background:#729aff;height:100%}
#kuBox .ku-list{display:grid;gap:8px}#kuBox .ku-listitem{border:1px solid #303747;border-radius:12px;padding:10px;overflow-wrap:anywhere}
#kuBox .ku-chart{display:flex;align-items:end;gap:3px;height:130px;padding:12px 0 20px;overflow-x:auto}
#kuBox .ku-chart i{display:block;min-width:8px;flex:1;background:#668cdb;border-radius:4px 4px 0 0;position:relative}
#kuBox .ku-chart i[title]:focus-visible{outline:2px solid white}
#kuBox .ku-chart i:last-child{background:#a6c0fb}
#kuBox .ku-field{display:grid;gap:5px;margin:10px 0}#kuBox .ku-field input,#kuBox .ku-field select{width:100%;min-height:44px;background:#111722;color:#f2f4ff;border:1px solid #4a5263;border-radius:9px;padding:8px}
#kuBox .ku-actions{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}
#kuBox .ku-actions button{flex:1;min-width:95px}#kuBox .ku-ticks{display:flex;justify-content:space-between;gap:3px}
#kuBox .ku-ticks button{min-width:0;flex:1;padding:7px 3px;font-size:11px;overflow-wrap:anywhere}
#kuBox .ku-ticks button[aria-pressed=true]{border-color:#84a8ff;background:#39588c}
#kuBox .ku-range{width:100%;height:52px;accent-color:#85aaff;touch-action:pan-y}
#kuBox .ku-range::-webkit-slider-thumb{width:26px;height:26px}
#kuBox .ku-checks{display:flex;flex-wrap:wrap;gap:8px}#kuBox .ku-checks label{display:flex;align-items:center;gap:4px;border:1px solid #343c4a;padding:8px;border-radius:8px;min-height:44px}
#kuBox .ku-checks input{width:18px;height:18px}#kuBox .ku-status{min-height:24px;color:#afc1db;margin:12px 0}
#kuBox .ku-status[data-error=true]{color:#ffab9f}#kuBox .ku-warning{color:#f8c894}
#kuBox .ku-model-title{font-weight:600;overflow-wrap:anywhere}#kuBox .ku-sheet-title{display:flex;justify-content:space-between;align-items:center;gap:8px}
#kuBox .ku-editor{border:1px solid #425274;border-radius:14px;padding:12px;margin:10px 0;background:#151d2a}
#kuBox .ku-pill{font-size:11px;color:#a9c4ff;border:1px solid #48618e;border-radius:8px;padding:2px 6px}
@media(max-width:360px){#kuBox .ku-grid{grid-template-columns:1fr}#kuBox .ku-tabs button{min-width:60px;font-size:12px}}
"""
BODY = r"""
<div class="ku-top"><h1>用量面板</h1><button type="button" data-ku="refresh">刷新</button><button type="button" data-ku="back">返回聊天</button></div>
<div class="ku-tabs" role="tablist" aria-label="面板分区">
<button type="button" data-tab="overview" aria-selected="true">概览</button><button type="button" data-tab="modes" aria-selected="false">模式</button><button type="button" data-tab="reports" aria-selected="false">报表</button><button type="button" data-tab="models" aria-selected="false">模型</button>
</div>
<section class="ku-pane" data-pane="overview"><div class="ku-grid" data-ku="cards"></div><div class="ku-card" data-ku="quota"></div><div class="ku-card" data-ku="misc"></div><div class="ku-card" data-ku="recent"></div></section>
<section class="ku-pane" data-pane="modes" hidden><div class="ku-card" data-ku="modePanel"></div><div class="ku-editor" data-ku="modeEditor" hidden></div></section>
<section class="ku-pane" data-pane="reports" hidden><div class="ku-period"><button type="button" data-period="today" aria-selected="true">今日逐时</button><button type="button" data-period="week" aria-selected="false">本周</button><button type="button" data-period="month" aria-selected="false">本月</button></div><div class="ku-card" data-ku="chart"></div><div class="ku-card" data-ku="modelStats"></div><div class="ku-card" data-ku="sessions"></div><div class="ku-card" data-ku="days"></div></section>
<section class="ku-pane" data-pane="models" hidden><div class="ku-card" data-ku="providers"></div><div class="ku-card" data-ku="modelList"></div><div class="ku-editor" data-ku="modelEditor" hidden></div></section>
<div class="ku-status" role="status" aria-live="polite" data-ku="status">加载中…</div>
"""
JS = r"""
function kimiPanelMount(box,root){
  var state={usage:null,modes:null,manager:null,audit:null,period:'today',modelPeriod:'today',tab:'overview',busy:false};
  var timer=null,alive=true,editing=false,epoch=0;
  function q(name){return box.querySelector('[data-ku="'+name+'"]');}
  function node(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=String(text);return e;}
  function clear(target){target.replaceChildren();return target;}
  function status(text,error){q('status').textContent=text;q('status').dataset.error=error?'true':'false';}
  function button(text,fn){var b=node('button',null,text);b.type='button';b.addEventListener('click',fn);return b;}
  function card(title,value,detail){var c=node('div','ku-card');c.append(node('h2',null,title),node('div','ku-big',value||'—'),node('div','ku-muted',detail||''));return c;}
  function row(k,v){var r=node('div','ku-row');r.append(node('span',null,k),node('span',null,v==null?'—':v));return r;}
  function field(label,value,type){var w=node('label','ku-field'),i=node('input');i.type=type||'text';i.value=value==null?'':value;w.append(node('span',null,label),i);return [w,i];}
  function select(label,options,current){var w=node('label','ku-field'),s=node('select');options.forEach(function(v){var o=node('option',null,v[1]);o.value=v[0];s.append(o)});s.value=current==null?'':current;w.append(node('span',null,label),s);return [w,s];}
  function checks(options,values){var wrap=node('div','ku-checks');options.forEach(function(x){var l=node('label'),c=node('input');c.type='checkbox';c.value=x[0];c.checked=values.indexOf(x[0])!==-1;l.append(c,node('span',null,x[1]));wrap.append(l)});return wrap;}
  function checked(w){return Array.from(w.querySelectorAll('input:checked')).map(function(e){return e.value});}
  function request(path,data){var ctrl=new AbortController(),opts={credentials:'same-origin',headers:{'Accept':'application/json'},signal:ctrl.signal};
    if(data!==undefined){opts.method='POST';opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(data)}
    var timeout=setTimeout(function(){ctrl.abort()},data===undefined?9000:22000);
    return fetch(root+path,opts).then(function(r){return r.json().then(function(d){
      if(r.status===403&&String(d.error||d.message||'').includes('未配对')){location.replace(root+'/mobile/pair');throw Error('配对会话已过期')}
      if(!r.ok||d.success===false)throw Error(d.message||d.error||'操作失败');return d})}).finally(function(){clearTimeout(timeout)})}
  function send(action,data,after){if(state.busy)return;state.busy=true;status('正在提交…');
    request('/mobile/panel/'+action,data).then(function(r){status(r.message||'已保存');if(after)after(r);return refresh()})
    .catch(function(e){status(e.message||'操作失败',true);renderModes()}).finally(function(){state.busy=false})}
  function refresh(){var current=epoch;return Promise.all([request('/mobile/usage/data'),request('/mobile/panel/modes'),request('/mobile/panel/manager'),request('/mobile/panel/audit')])
    .then(function(x){if(!alive||current!==epoch)return;state.usage=x[0];state.modes=x[1];state.manager=x[2];state.audit=x[3];renderAll()})
    .catch(function(e){if(alive&&current===epoch)status(e.message||'电脑面板暂时不可用',true)})}
  function renderAll(){renderOverview();renderModes();renderReports();renderManager();if(!editing)status(state.usage&&state.usage._stale?'用量为缓存，采集于 '+(state.usage._collected_at||state.usage.updated_at):'采集于 '+(state.usage&&state.usage.updated_at||'未知'),false)}
  function renderOverview(){var d=state.usage||{},cards=clear(q('cards'));
    [['今日',d.today],['昨日',d.yesterday],['本周',d.week],['本月',d.month],['累计',d.cumul]].forEach(function(x){var v=x[1]||{};cards.append(card(x[0],v.tokens_fmt,(v.cost_fmt||'')+' · '+(v.calls||0)+' 次'))});
    var quota=clear(q('quota'));quota.append(node('h2',null,'官方配额'));
    ['week','h5'].forEach(function(k){var v=(d.quota||{})[k];if(!v)return;quota.append(row(k==='week'?'每周':'5 小时',(v.pct==null?'—':v.pct.toFixed(1)+'%')+' · '+(v.used||0)+' / '+(v.limit||0)));var bar=node('div','ku-bar'),fill=node('i');fill.style.width=Math.max(0,Math.min(100,v.pct||0))+'%';bar.append(fill);quota.append(bar);if(v.eta)quota.append(row('预计耗尽',v.eta))});
    if(quota.childElementCount===1)quota.append(node('div','ku-muted','当前账号没有配额数据'));
    var misc=clear(q('misc'));misc.append(node('h2',null,'实时指标'));var rate=d.rate||{},speed=d.speed||{},cur=d.cur||{};
    [['当前模型',cur.alias],['思考强度',cur.effort],['消耗速率',rate.tokens_per_hour],['成本速率',rate.cost_per_hour],['缓存命中',d.cache&&d.cache.pct_fmt],['输出速度',speed.tps],['历史均速',speed.avg_tps],['本会话',d.session&&d.session.tokens_fmt]].forEach(function(x){if(x[1])misc.append(row(x[0],x[1]))});
    var recent=clear(q('recent'));recent.append(node('h2',null,'今日模型'));(d.today_models||[]).slice(0,8).forEach(function(m){recent.append(row(m.model,(m.tokens_fmt||'—')+' · '+(m.cost_fmt||'—')))});
  }
  function modeRequest(index){var body={index:index},sid='';
    try{var app=document.querySelector('#app').__vue_app__,pinia=app.config.globalProperties.$pinia;
      sid=pinia._s.get('kimi.sessions').activeSessionId||''}catch(e){}
    if(!sid){var match=location.pathname.match(/\/(?:sessions|session)\/(session_[A-Za-z0-9_-]{8,100})(?:\/|$)/);sid=match?match[1]:''}
    if(typeof sid==='string'&&/^session_[A-Za-z0-9_-]{8,100}$/.test(sid))body.session_id=sid;
    return body}
  function modeOptions(){return (state.modes&&state.modes.models||[]).map(function(m){return [m.alias,m.display_name||m.alias]})}
  function renderModes(){var boxMode=clear(q('modePanel')),m=state.modes;if(!m){boxMode.textContent='正在加载档位…';return}
    var modes=m.modes||[],index=typeof m.active==='number'?m.active:typeof m.last_applied?.index==='number'?m.last_applied.index:0;
    var selected=modes[index]||modes[0],title=node('h2',null,'滑动变阻器 · '+(selected?selected.name:'未匹配'));
    boxMode.append(title,node('div','ku-muted',selected?(selected.scene||'')+' · 主模型 '+selected.main+' · '+(selected.subagents||[]).length+' 个子代理':'当前配置不匹配预设'));
    if(!modes.length)return;
    var slider=node('input','ku-range');slider.type='range';slider.min=0;slider.max=modes.length-1;slider.step=1;slider.value=index;slider.setAttribute('aria-label','选择模式档位');
    var preview=node('div','ku-muted','拖动仅预览，松手后确认应用');slider.addEventListener('input',function(){var v=modes[Number(slider.value)];preview.textContent=v.name+' · '+v.main+' · '+(v.subagents||[]).length+' 个子代理'});
    function apply(i){var target=modes[i];if(!target)return;
      if(confirm((i===m.active?'重新同步':'切换至')+'「'+target.name+'」？\n主模型 '+target.main+' · '+(target.subagents||[]).length+' 个子代理'))send('apply',modeRequest(i));else slider.value=index}
    slider.addEventListener('change',function(){apply(Number(slider.value))});boxMode.append(slider,preview);
    var ticks=node('div','ku-ticks');modes.forEach(function(v,i){var b=button(v.name,function(){apply(i)});b.setAttribute('aria-pressed',i===m.active?'true':'false');ticks.append(b)});boxMode.append(ticks);
    var actions=node('div','ku-actions'),locked=!!(m.locked&&m.locked.locked);
    actions.append(button(locked?'解锁':'锁定',function(){if(confirm(locked?'解锁当前档位？':'锁定此档并校准运行中的会话及新会话默认模型？'))send(locked?'unlock':'lock',locked?{}:modeRequest(index))}));
    actions.append(button('编辑档位',function(){openModeEditor()}));
    if(m.has_snapshot)actions.append(button('恢复切档前配置',function(){if(confirm('恢复切档前的模型配置？'))send('restore',{})}));boxMode.append(actions);
    if(locked)boxMode.append(node('div','ku-warning','已锁定：手机不能直接改其他主模型；消息发送前会核对目标会话，校准失败不会发送。切档或解锁请使用本面板。'));
  }
  function openModeEditor(){var wrap=clear(q('modeEditor')),m=state.modes;if(!m)return;editing=true;wrap.hidden=false;
    var modes=JSON.parse(JSON.stringify(m.modes||[]));wrap.append(node('h2',null,'编辑模式档位'));
    var list=node('div');wrap.append(list);
    function redraw(){clear(list);modes.forEach(function(v,i){var c=node('div','ku-card');c.append(node('h2',null,'档位 '+(i+1)));
      var name=field('名称',v.name),scene=field('场景描述',v.scene),main=select('主模型',modeOptions(),v.main),effort=select('思考强度',[['','跟随模型'],['off','关闭'],['on','开启'],['low','低'],['medium','中'],['high','高'],['xhigh','极高'],['max','最大']],v.effort);
      var subs=checks(modeOptions(),v.subagents||[]);c.append(name[0],scene[0],main[0],effort[0],node('div','ku-muted','子代理模型（最多 3 个）'),subs);
      name[1].oninput=function(){v.name=this.value};scene[1].oninput=function(){v.scene=this.value};main[1].onchange=function(){v.main=this.value};effort[1].onchange=function(){v.effort=this.value};
      subs.onchange=function(){v.subagents=checked(subs)};
      var order=node('div','ku-actions');if(i>0)order.append(button('上移',function(){var x=modes.splice(i,1)[0];modes.splice(i-1,0,x);redraw()}));if(i<modes.length-1)order.append(button('下移',function(){var x=modes.splice(i,1)[0];modes.splice(i+1,0,x);redraw()}));if(modes.length>2)order.append(button('移除档位',function(){modes.splice(i,1);redraw()}));c.append(order);list.append(c)})}
    redraw();var actions=node('div','ku-actions');actions.append(button('新增档位',function(){if(modes.length>=6){status('最多 6 个档位',true);return}modes.push({name:'新档位',scene:'',main:(modeOptions()[0]||[''])[0],subagents:[],effort:'high'});redraw()}));
    actions.append(button('保存',function(){if(modes.some(function(v){return !v.name||!v.scene||!v.main||v.subagents.length>3})){status('请完善名称、场景、主模型与子代理',true);return}send('save',{modes:modes},function(){editing=false;wrap.hidden=true})}));
    actions.append(button('恢复默认档位',function(){if(confirm('将自定义档位重置为内置预设？'))send('reset',{},function(){editing=false;wrap.hidden=true})}));
    actions.append(button('取消',function(){editing=false;wrap.hidden=true;renderAll()}));wrap.append(actions)
  }
  function renderReports(){var d=state.usage||{},chart=clear(q('chart'));chart.append(node('h2',null,{'today':'逐小时调用','week':'近 7 天走势','month':'近 30 天走势'}[state.period]));
    var points=((((d.timeseries||{})[state.period]||{}).points)||[]),max=Math.max(1,...points.map(function(p){return p.tokens||0}));
    var graph=node('div','ku-chart');graph.setAttribute('aria-label','Token 用量趋势');var detail=node('div','ku-muted','点击柱形查看具体用量');
    points.forEach(function(p){var bar=node('i');bar.style.height=Math.max(3,Math.round((p.tokens||0)/max*100))+'%';bar.title=p.label+' · '+p.tokens+' Token · '+p.calls+' 次';bar.tabIndex=0;bar.setAttribute('role','button');bar.setAttribute('aria-label',bar.title);bar.onclick=function(){detail.textContent=bar.title};bar.onkeydown=function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();bar.click()}};graph.append(bar)});chart.append(graph,detail);
    var models=clear(q('modelStats'));models.append(node('h2',null,'模型份额'));
    var periods=node('div','ku-period');[['today','今日'],['yesterday','昨日'],['week','本周'],['month','本月'],['cumul','累计']].forEach(function(pair){var b=button(pair[1],function(){state.modelPeriod=pair[0];renderReports()});b.setAttribute('aria-selected',state.modelPeriod===pair[0]?'true':'false');periods.append(b)});models.append(periods);
    var entries=state.modelPeriod==='cumul'?(d.cumul_models||[]):((d[state.modelPeriod]||{}).models||[]);
    entries.forEach(function(x){models.append(row(x.model,(x.tokens_fmt||'—')+' · '+(x.calls||0)+' 次 · 入 '+(x.in_fmt||'—')+' / 出 '+(x.out_fmt||'—')+' · 缓存 '+(x.cache_pct||0)+'% · '+(x.cost_fmt||'—')))});
    var sessions=clear(q('sessions'));sessions.append(node('h2',null,'会话 Top 8'));(d.sessions||[]).forEach(function(x){sessions.append(row(x.short||'会话',(x.tokens_fmt||'—')+' · '+(x.cost_fmt||'—')))});
    var days=clear(q('days'));days.append(node('h2',null,'近 30 天明细'));(d.days30||[]).slice().reverse().forEach(function(x){days.append(row(x.date||x.label,(x.tokens_fmt||'—')+' · '+(x.calls||0)+' 次 · ¥'+Number(x.cost||0).toFixed(2)))});
  }
  function renderManager(){var m=state.manager;if(!m)return;var providers=clear(q('providers')),models=clear(q('modelList'));
    providers.append(node('h2',null,'供应商'));if(!m.editable)providers.append(node('div','ku-warning',m.message||'配置暂不可编辑'));
    (m.providers||[]).forEach(function(p){var c=node('div','ku-listitem'),heading=node('div','ku-sheet-title');heading.append(node('span','ku-model-title',p.name+' · '+p.type),button(p.managed?'只读':'编辑',function(){openProvider(p)}));heading.lastChild.disabled=!!p.managed;c.append(heading,node('div','ku-muted',p.base_url||'未配置地址'),node('div','ku-muted',p.has_api_key?'密钥已保存（不可回显）':'未设密钥'));providers.append(c)});
    providers.append(button('新增供应商',function(){openProvider(null)}));models.append(node('h2',null,'模型与能力'));
    var search=field('搜索模型别名、名称或供应商','');models.append(search[0]);var list=node('div','ku-list');models.append(list);
    function draw(){clear(list);(m.models||[]).filter(function(x){return (x.alias+' '+x.display_name+' '+x.provider).toLowerCase().includes(search[1].value.toLowerCase())}).forEach(function(x){var c=node('div','ku-listitem');c.append(node('div','ku-model-title',x.display_name||x.alias),node('div','ku-muted',x.alias+' · '+x.provider+' · '+(x.max_context_size||0)+' 上下文'));
      var act=node('div','ku-actions');if(!x.managed)act.append(button('编辑',function(){openModel(x)}));else act.append(node('span','ku-pill','托管只读'));act.append(button('计价',function(){openPrice(x)}));if(!x.delete_protected&&!x.managed)act.append(button('删除',function(){if(confirm('永久删除模型 '+x.alias+' ？'))send('batch',{version:m.version,provider:x.provider,upserts:[],removes:[x.alias]})}));c.append(act);
      var audit=((state.audit&&state.audit.models||[]).find(function(a){return a.alias===x.alias})||{}).issues||[];
      audit.filter(function(it){return it.code&&it.level!=='info'}).forEach(function(it){var issue=node('div','ku-muted',(it.dismissed?'已忽略 · ':'')+it.msg),controls=node('div','ku-actions');if(it.fix&&!it.dismissed)controls.append(button(it.fix,function(){if(confirm('修复 '+x.alias+' 的问题？'))send('issue',{action:'fix',alias:x.alias,code:it.code})}));controls.append(button(it.dismissed?'恢复提示':'忽略提示',function(){send('issue',{action:it.dismissed?'unignore':'ignore',alias:x.alias,code:it.code})}));c.append(issue,controls)});list.append(c)})}
    search[1].oninput=draw;draw();var actions=node('div','ku-actions');actions.append(button('新增模型',function(){openModel(null)}),button('同步参考价格',function(){matchPrices()}));
    actions.append(button('补全全部能力',function(){if(confirm('为本机模型补全默认能力标签？'))send('enable-all',{})}));
    actions.append(button('修复可自动处理的问题',function(){if(confirm('批量修复所有可自动处理的模型问题？'))send('issue',{action:'fix_all'})}));models.append(actions);
  }
  function editor(title){var w=clear(q('modelEditor'));w.hidden=false;editing=true;w.append(node('h2',null,title));return w}
  function closeEditor(){editing=false;q('modelEditor').hidden=true;renderAll()}
  function openProvider(p){var w=editor(p?'编辑供应商':'新增供应商'),m=state.manager;
    var name=field('供应商名称',p&&p.name),type=select('协议',(m.provider_types||[]).map(function(v){return [v,v]}),p&&p.type),url=field('API 地址',p&&p.base_url,'url'),action=select('密钥处理',[['keep','保留原值'],['replace','替换为新密钥'],['clear','清空密钥']],'keep'),key=field('新密钥（不回显原值）','','password');
    w.append(name[0],type[0],url[0],action[0],key[0]);if(p)name[1].disabled=true;
    if(p&&p.url_hidden)w.append(node('div','ku-warning','原地址含自定义路径，已隐藏；编辑时须重新填写完整地址，否则请取消。'));
    if(location.protocol!=='https:'){action[1].querySelector('option[value="replace"]').disabled=true;key[1].disabled=true;w.append(node('div','ku-warning','当前连接为明文 HTTP：禁止在手机提交新密钥，请在电脑端设置。'))}
    var buttons=node('div','ku-actions');buttons.append(button('保存',function(){if(p&&p.url_hidden&&!url[1].value){status('请填写原供应商完整地址，避免清空现有设置',true);return}var item={name:name[1].value,type:type[1].value,base_url:url[1].value,key_action:action[1].value};if(item.key_action==='replace')item.api_key=key[1].value;
      if(!confirm('保存供应商配置？'))return;send('provider',{version:m.version,original_name:p?p.name:null,provider:item},closeEditor)}),button('取消',closeEditor));w.append(buttons)
  }
  function openModel(old){var w=editor(old?'编辑模型':'新增模型'),m=state.manager,provider=select('供应商',(m.providers||[]).filter(function(p){return !p.managed}).map(function(p){return [p.name,p.name]}),old&&old.provider);
    var alias=field('模型别名',old&&old.alias),id=field('模型 ID',old&&old.model),name=field('显示名称',old&&old.display_name),ctx=field('上下文长度',old&&old.max_context_size||1000000,'number');
    var caps=checks([['image_in','识图'],['video_in','视频'],['thinking','思考'],['always_thinking','始终思考'],['tool_use','工具'],['dynamically_loaded_tools','动态工具']],old?old.capabilities:['image_in','thinking','tool_use']);
    var efforts=checks([['low','low'],['medium','medium'],['high','high'],['xhigh','xhigh'],['max','max']],old?old.support_efforts:['low','medium','high','xhigh','max']);
    var def=select('默认思考档位',[['','跟随模型'],['low','low'],['medium','medium'],['high','high'],['xhigh','xhigh'],['max','max']],old?(old.default_effort||''):'high');
    w.append(provider[0],alias[0],id[0],name[0],ctx[0],node('div','ku-muted','能力标签'),caps,node('div','ku-muted','支持强度'),efforts,def[0]);if(old){provider[1].disabled=true;alias[1].disabled=true}
    var buttons=node('div','ku-actions');buttons.append(button('保存模型',function(){var item={alias:alias[1].value,provider:provider[1].value,model:id[1].value,display_name:name[1].value,max_context_size:Number(ctx[1].value),capabilities:checked(caps),support_efforts:checked(efforts),default_effort:def[1].value||null};
      if(!confirm('保存模型 '+item.alias+'？'))return;send('model',{version:m.version,original_alias:old?old.alias:null,model:item},closeEditor)}));
    if(old&&!old.managed)buttons.append(button('设为默认',function(){if(confirm('设置为默认模型？'))send('default',{alias:old.alias},closeEditor)}));
    if(!old)buttons.append(button('探测上游列表',function(){send('catalog',{version:m.version,provider:provider[1].value},function(r){if(r.models&&r.models.length){var list=select('上游模型',r.models.map(function(x){var id=x.id||x.model||'';return [id,x.name||x.display_name||id]}),'');w.insertBefore(list[0],alias[0]);list[1].onchange=function(){id[1].value=this.value;alias[1].value=provider[1].value+'/'+this.value;name[1].value=list[1].selectedOptions[0].textContent}}})}));
    buttons.append(button('取消',closeEditor));w.append(buttons)
  }
  function openPrice(model){var pricing=((state.modes&&state.modes.models||[]).find(function(x){return x.alias===model.alias})||{}).pricing||{};
    var w=editor('模型计价 · '+model.alias),mode=select('方式',[['volume','每百万 Token'],['per_call','每次调用'],['','恢复默认']],pricing.mode||'volume');w.append(mode[0]);
    var fields={};[['input','输入'],['output','输出'],['cache_hit','缓存读取'],['cache_write','缓存写入'],['price','每次价格']].forEach(function(x){var f=field(x[1],pricing[x[0]]==null?0:pricing[x[0]],'number');f[1].min=0;f[1].step='any';fields[x[0]]=f[1];w.append(f[0])});
    var actions=node('div','ku-actions');actions.append(button('保存价格',function(){var req={alias:model.alias,mode:mode[1].value};if(req.mode==='per_call')req.price=Number(fields.price.value);if(req.mode==='volume')for(var k of ['input','output','cache_hit','cache_write'])req[k]=Number(fields[k].value);if(confirm('更新 '+model.alias+' 的计价？'))send('price',req,closeEditor)}),button('取消',closeEditor));w.append(actions)
  }
  function matchPrices(){if(!confirm('从 models.dev 获取公开牌价供你确认？'))return;send('match-prices',{rate:7.1},function(r){var w=editor('参考价格匹配');var rows=(r.rows||[]).filter(function(x){return x.cost&&x.alias});if(!rows.length){w.append(node('div','ku-muted','没有可匹配的模型'));return}
    rows.forEach(function(x){var c=node('label','ku-listitem'),check=node('input');check.type='checkbox';check.checked=!!x.exact;x._check=check;c.append(check,node('span',null,' '+x.alias+' → '+x.matched_id+' · '+JSON.stringify(x.cost)));w.append(c)});
    var actions=node('div','ku-actions');actions.append(button('确认写入所选价格',function(){var chosen=rows.filter(function(x){return x._check.checked}).map(function(x){return {alias:x.alias,selected:true,cost:x.cost}});if(chosen.length&&confirm('将 '+chosen.length+' 个模型的价格写入本机？'))send('apply-prices',{rows:chosen},closeEditor)}),button('取消',closeEditor));w.append(actions)})}
  box.querySelector('[data-ku="refresh"]').addEventListener('click',function(){if(!editing)refresh()});
  box.querySelectorAll('[data-tab]').forEach(function(b){b.addEventListener('click',function(){state.tab=b.dataset.tab;box.querySelectorAll('[data-tab]').forEach(function(t){t.setAttribute('aria-selected',t===b?'true':'false')});box.querySelectorAll('[data-pane]').forEach(function(p){p.hidden=p.dataset.pane!==state.tab});if(state.tab==='models')refresh()})});
  box.querySelectorAll('[data-period]').forEach(function(b){b.addEventListener('click',function(){state.period=b.dataset.period;box.querySelectorAll('[data-period]').forEach(function(t){t.setAttribute('aria-selected',t===b?'true':'false')});renderReports()})});
  function poll(){timer=null;if(!alive||document.hidden)return;if(editing){timer=setTimeout(poll,5000);return}var current=epoch;refresh().finally(function(){if(alive&&current===epoch)timer=setTimeout(poll,5000)})}
  function visible(){if(document.hidden){epoch++;if(timer)clearTimeout(timer);timer=null}else if(alive&&!timer)poll()}
  document.addEventListener('visibilitychange',visible);poll();
  return {stop:function(){alive=false;epoch++;if(timer)clearTimeout(timer);timer=null;document.removeEventListener('visibilitychange',visible)},start:function(){if(!alive){alive=true;epoch++;document.addEventListener('visibilitychange',visible)}if(!timer&&!document.hidden)poll()}};
}
"""
PAGE_HTML = '<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="referrer" content="no-referrer"><title>Kimi Code · 手机面板</title><style>*{box-sizing:border-box}body{background:#101116;margin:0;padding:10px max(12px,env(safe-area-inset-right)) max(16px,env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left))}' + CSS + '</style></head><body><div id="kuBox">' + BODY + '</div><script>(function(){"use strict";' + JS + 'var root=location.pathname.replace(/\\/mobile\\/usage\\/?$/,""),box=document.getElementById("kuBox");kimiPanelMount(box,root);box.querySelector("[data-ku=back]").onclick=function(){location.href=root+"/"};})();</script></body></html>'
DRAG_JS = r"""
function kimiDragPanel(btn,toggle,overlay){
  var key='ku-mobile-panel-position-v1',drag=null,suppress=false,hold=null;
  var defaultRight=btn.style.right,defaultBottom=btn.style.bottom;
  var probe=document.createElement('div');
  probe.style.cssText='position:fixed;visibility:hidden;pointer-events:none;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)';
  document.body.appendChild(probe);
  function bounds(){
    var style=getComputedStyle(probe),view=window.visualViewport;
    var left=view?view.offsetLeft:0,top=view?view.offsetTop:0;
    var width=view?view.width:window.innerWidth,height=view?view.height:window.innerHeight;
    var rect=btn.getBoundingClientRect();
    var x0=left+(parseFloat(style.paddingLeft)||0)+8,y0=top+(parseFloat(style.paddingTop)||0)+8;
    var x1=Math.max(x0,left+width-rect.width-(parseFloat(style.paddingRight)||0)-8);
    var y1=Math.max(y0,top+height-rect.height-(parseFloat(style.paddingBottom)||0)-8);
    return {x0:x0,y0:y0,x1:x1,y1:y1};
  }
  function clamp(value,min,max){return Math.min(max,Math.max(min,value))}
  function place(x,y,save){
    var b=bounds(),px=clamp(x,b.x0,b.x1),py=clamp(y,b.y0,b.y1);
    btn.style.right='auto';btn.style.bottom='auto';btn.style.left=px+'px';btn.style.top=py+'px';
    if(save){try{localStorage.setItem(key,JSON.stringify({x:(px-b.x0)/Math.max(1,b.x1-b.x0),y:(py-b.y0)/Math.max(1,b.y1-b.y0)}))}catch(e){}}
  }
  function restore(){
    if(drag)return;
    try{var saved=JSON.parse(localStorage.getItem(key));
      if(saved&&typeof saved.x==='number'&&typeof saved.y==='number'&&
         isFinite(saved.x)&&isFinite(saved.y)&&saved.x>=0&&saved.x<=1&&saved.y>=0&&saved.y<=1){
        var b=bounds();place(b.x0+saved.x*(b.x1-b.x0),b.y0+saved.y*(b.y1-b.y0),false);return}}
    catch(e){}
    btn.style.left='';btn.style.top='';btn.style.right=defaultRight;btn.style.bottom=defaultBottom;
  }
  function release(e){
    if(!drag||e.pointerId!==drag.id)return;
    clearTimeout(hold);hold=null;
    if(drag.moved){var r=btn.getBoundingClientRect();place(r.left,r.top,true);suppress=true}
    else if(drag.reset)suppress=true;
    if(suppress)setTimeout(function(){suppress=false},400);
    try{btn.releasePointerCapture(drag.id)}catch(err){}
    drag=null;
  }
  btn.style.touchAction='none';btn.style.userSelect='none';btn.style.webkitUserSelect='none';
  btn.style.webkitTouchCallout='none';
  btn.addEventListener('pointerdown',function(e){
    if(drag||e.button!==0||e.isPrimary===false)return;
    var r=btn.getBoundingClientRect();drag={id:e.pointerId,x:e.clientX,y:e.clientY,left:r.left,top:r.top,moved:false,reset:false};
    try{btn.setPointerCapture(e.pointerId)}catch(err){}
    hold=setTimeout(function(){if(!drag||drag.moved)return;
      drag.reset=true;suppress=true;
      try{localStorage.removeItem(key)}catch(err){}
      btn.style.left='';btn.style.top='';btn.style.right=defaultRight;btn.style.bottom=defaultBottom;
      btn.textContent='位置已复位';setTimeout(function(){btn.textContent='面板'},1100);
    },650);
  });
  btn.addEventListener('pointermove',function(e){
    if(!drag||e.pointerId!==drag.id||drag.reset)return;
    var dx=e.clientX-drag.x,dy=e.clientY-drag.y;
    if(!drag.moved&&Math.hypot(dx,dy)<9)return;
    drag.moved=true;clearTimeout(hold);hold=null;
    place(drag.left+dx,drag.top+dy,false);
  });
  btn.addEventListener('pointerup',release);
  btn.addEventListener('pointercancel',function(e){if(!drag||e.pointerId!==drag.id)return;
    clearTimeout(hold);hold=null;drag=null;suppress=false;restore()});
  btn.addEventListener('click',function(e){if(suppress){suppress=false;e.preventDefault();e.stopImmediatePropagation();return}toggle()},true);
  function keyboard(){var a=document.activeElement,edit=a&&(a.isContentEditable||a.tagName==='TEXTAREA'||(a.tagName==='INPUT'&&/^(text|search|password|email|number|url)$/.test(a.type)));
    var view=window.visualViewport,raised=view&&window.innerHeight-view.height>100;
    btn.style.visibility=(overlay.hidden&&!edit||overlay.hidden&&!raised)?'visible':'hidden';}
  function resize(){restore();keyboard()}
  window.addEventListener('resize',resize);
  window.addEventListener('orientationchange',resize);
  if(window.visualViewport)window.visualViewport.addEventListener('resize',resize);
  document.addEventListener('focusin',keyboard);
  document.addEventListener('focusout',function(){setTimeout(keyboard,80)});
  restore();keyboard();
}
function kimiSheetDrag(overlay){
  var sheet=overlay.querySelector('.ku-sheet'),handle=sheet.querySelector('.ku-top');
  var key='ku-mobile-panel-sheet-position-v1',drag=null;
  handle.style.touchAction='none';
  function viewport(){return window.visualViewport?window.visualViewport.height:window.innerHeight}
  function move(top,save){var height=viewport(),min=height*.04,max=Math.max(min,Math.min(height*.7,height-200));
    var value=Math.max(min,Math.min(max,top));sheet.style.top=value+'px';
    if(save){try{localStorage.setItem(key,String(value/Math.max(1,height)))}catch(e){}}
  }
  function restore(){if(drag)return;try{var value=Number(localStorage.getItem(key));
    if(isFinite(value)&&value>=.04&&value<=.7){move(value*viewport(),false);return}}
    catch(e){}sheet.style.top=''}
  handle.addEventListener('pointerdown',function(e){if(e.button!==0||e.target.closest('button,input,select'))return;
    drag={id:e.pointerId,y:e.clientY,top:sheet.getBoundingClientRect().top,moved:false};
    try{handle.setPointerCapture(e.pointerId)}catch(err){}
  });
  handle.addEventListener('pointermove',function(e){if(!drag||drag.id!==e.pointerId)return;
    var dy=e.clientY-drag.y;if(!drag.moved&&Math.abs(dy)<8)return;
    drag.moved=true;move(drag.top+dy,false)
  });
  function end(e){if(!drag||drag.id!==e.pointerId)return;
    if(drag.moved)move(sheet.getBoundingClientRect().top,true);
    try{handle.releasePointerCapture(e.pointerId)}catch(err){}
    drag=null}
  handle.addEventListener('pointerup',end);
  handle.addEventListener('pointercancel',function(e){if(drag&&drag.id===e.pointerId){drag=null;restore()}});
  window.addEventListener('resize',restore);
  if(window.visualViewport)window.visualViewport.addEventListener('resize',restore);
  restore();
}
function kimiNativeModeLock(root,btn){
  var locked=false,pending=false;
  function paint(){btn.textContent=locked?'面板 · 已锁':'面板';
    document.querySelectorAll('.model-pill,.ku-phone-model-locked').forEach(function(el){el.classList.toggle('ku-phone-model-locked',locked)})}
  function poll(){if(document.hidden||pending)return;pending=true;var ctrl=new AbortController(),timeout=setTimeout(function(){ctrl.abort()},4500);
    fetch(root+'/mobile/panel/modes',{credentials:'same-origin',signal:ctrl.signal}).then(function(r){if(!r.ok)throw Error();return r.json()})
      .then(function(data){locked=!!(data.locked&&data.locked.locked);paint()})
      .catch(function(){}).finally(function(){clearTimeout(timeout);pending=false})}
  function block(e){if(!locked||!e.target.closest||!e.target.closest('.model-pill'))return;
    e.preventDefault();e.stopImmediatePropagation();btn.textContent='面板 · 已锁'}
  document.addEventListener('pointerdown',block,true);
  document.addEventListener('click',block,true);
  document.addEventListener('visibilitychange',function(){if(!document.hidden)poll()});
  setInterval(poll,2500);poll();
}
"""
OVERLAY_HTML = '<style>.ku-phone-model-locked{opacity:.65;cursor:not-allowed}#kuOv{position:fixed;inset:0;z-index:10000}#kuOv[hidden]{display:none}#kuOv .ku-mask{position:absolute;inset:0;background:rgba(0,0,0,.7)}#kuOv .ku-sheet{position:absolute;inset:4dvh 0 0;background:#101116;border-radius:18px 18px 0 0;overflow-y:auto;overscroll-behavior:contain;padding:12px max(12px,env(safe-area-inset-right)) max(20px,env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left))}#kuOv .ku-top{touch-action:none;cursor:ns-resize}#kuOv .ku-top:before{content:"";position:absolute;top:0;left:calc(50% - 20px);width:40px;height:4px;border-radius:4px;background:#75829d}@supports not (height:1dvh){#kuOv .ku-sheet{inset:4% 0 0}}</style><style>' + CSS + '</style><div id="kuOv" hidden><div class="ku-mask"></div><div class="ku-sheet"><div id="kuBox">' + BODY + '</div></div></div><button id="kuUsageBtn" type="button" style="position:fixed;right:max(12px,env(safe-area-inset-right));bottom:max(12px,env(safe-area-inset-bottom));z-index:10001;min-height:48px;touch-action:none;user-select:none;padding:10px 16px;border-radius:99px;background:#405eac;color:white;border:1px solid #87a5f5;font:14px system-ui">面板</button><script>(function(){"use strict";' + JS + DRAG_JS + 'var m=location.pathname.match(/^(\\/t\\/[A-Za-z0-9_-]{4,32})(?=\\/|$)/),root=m?m[1]:"",ov=document.getElementById("kuOv"),btn=document.getElementById("kuUsageBtn"),inst=null;function open(){ov.hidden=false;btn.style.visibility="hidden";if(!inst)inst=kimiPanelMount(document.getElementById("kuBox"),root);else inst.start()}function close(){ov.hidden=true;btn.style.visibility="visible";if(inst)inst.stop()}kimiDragPanel(btn,function(){ov.hidden?open():close()},ov);kimiSheetDrag(ov);kimiNativeModeLock(root,btn);ov.querySelector(".ku-mask").onclick=close;ov.querySelector("[data-ku=back]").onclick=close})();</script>'
