const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const pad = n => String(n).padStart(2, '0');
const localDay = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
const zoned = (value, zone) => {
  if (!value) return {day:'',time:''};
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value)).map(x => [x.type,x.value]));
  return {day:`${p.year}-${p.month}-${p.day}`,time:`${p.hour}:${p.minute}`};
};
const onDay = (task, day, zone) => {
  if (task.due_date) return task.due_date<=day && (task.end_date ? day<task.end_date : task.due_date===day);
  if (!task.due_at) return false;
  const start = zoned(task.due_at, zone), end = zoned(task.end_at || task.due_at, zone);
  return start.day === day || (start.day < day && (end.day > day || (end.day === day && end.time !== '00:00')));
};
const monthDays = date => {
  const first = new Date(date.getFullYear(), date.getMonth(), 1);
  first.setDate(first.getDate() - (first.getDay()+6)%7);
  return Array.from({length:42}, (_,i) => new Date(first.getFullYear(),first.getMonth(),first.getDate()+i));
};
const taskDay = (task,zone) => task.due_date || zoned(task.due_at,zone).day;
const taskOrder = (task,zone) => task.due_date ? task.due_date+'T00:00' : task.due_at ? taskDay(task,zone)+'T'+zoned(task.due_at,zone).time : '9999';
const palette = ['#6286da','#39a594','#bc8a42','#a773c6','#d57483','#569bb8'];

class PersonalSecretaryPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode:'open'});
    this.month = new Date(); this.view = 'calendar'; this.category = ''; this.priority = ''; this.draft = '';
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;height:100%;overflow:auto;background:var(--primary-background-color);color:var(--primary-text-color);font:15px var(--paper-font-body1_-_font-family,Roboto,Arial,sans-serif)}
      *{box-sizing:border-box}button,input,textarea,select{font:inherit}button{cursor:pointer;color:inherit;border:0}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--primary-color);outline-offset:2px}
      .app{max-width:1440px;margin:auto;padding:24px 28px 48px}.header,.toolbar,.row,.nav,.filters{display:flex;align-items:center;gap:12px}.header{margin-bottom:24px}.brand{width:48px;height:48px;border-radius:12px;flex-shrink:0}.header h1{font-size:28px;letter-spacing:-.8px;margin:0}.header p{margin:5px 0 0;color:var(--secondary-text-color)}.grow{flex:1}.icon{background:transparent;font-size:22px;padding:8px;border-radius:12px}.icon:hover,.pill:hover{background:var(--secondary-background-color)}
      .toolbar{flex-wrap:wrap;margin-bottom:16px}.nav{gap:6px}.nav strong{min-width:180px;text-align:center;font-size:18px}.pill{border-radius:18px;background:var(--card-background-color);padding:9px 15px;white-space:nowrap}.pill.selected{background:var(--primary-color);color:var(--text-primary-color,#fff)}.filters{gap:8px;overflow:auto;padding-bottom:12px;margin-bottom:8px}.filters select{padding:8px;border-radius:12px;background:var(--card-background-color);color:inherit;border:1px solid var(--divider-color)}
      .board{border:1px solid var(--divider-color);border-radius:18px;overflow:hidden;background:var(--card-background-color);margin-bottom:24px}.weekdays,.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}.weekdays span{padding:12px;color:var(--secondary-text-color);font-size:12px;text-align:right}.day{min-height:112px;padding:8px;border-top:1px solid var(--divider-color);border-right:1px solid var(--divider-color);overflow:hidden}.day:nth-child(7n){border-right:0}.day.muted{background:var(--secondary-background-color)}.day.muted .date{opacity:.45}.date{display:block;margin-left:auto;padding:5px 8px;background:transparent;border-radius:50%;font-size:13px}.date.today{background:var(--primary-color);color:white}.date.picked{outline:2px solid var(--primary-color)}
      .event{display:block;width:100%;text-align:left;background:color-mix(in srgb,var(--tag) 15%,transparent);border-left:3px solid var(--tag);border-radius:5px;padding:5px 6px;margin-top:5px;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.event.important{font-weight:700}.event.completed{text-decoration:line-through;opacity:.55}.event time{margin-right:5px;opacity:.75}.more{font-size:11px;margin:5px 0;color:var(--secondary-text-color)}
      .agenda h2{font-size:18px;margin:24px 0 12px}.task{display:flex;gap:12px;align-items:center;padding:15px 4px;border-bottom:1px solid var(--divider-color)}.check{background:transparent;border:2px solid var(--divider-color);border-radius:50%;width:24px;height:24px;flex-shrink:0;padding:0}.check.done{background:var(--primary-color);border-color:var(--primary-color);color:#fff}.taskmain{background:transparent;text-align:left;flex:1;min-width:0}.tasktitle{font-size:15px;overflow-wrap:anywhere}.tasktitle.done{text-decoration:line-through;opacity:.55}.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.tag{font-size:11px;border-radius:6px;background:color-mix(in srgb,var(--tag) 14%,transparent);color:var(--primary-text-color);padding:3px 7px}.when{font-size:12px;color:var(--secondary-text-color);text-align:right;white-space:nowrap}.empty{padding:24px 4px;color:var(--secondary-text-color)}
      .composer{display:flex;gap:8px;border:1px solid var(--divider-color);border-radius:16px;padding:10px;background:var(--card-background-color);margin:24px 0 8px}.composer textarea{resize:vertical;min-height:40px;max-height:160px;flex:1;border:0;background:transparent;color:inherit;padding:8px;outline-offset:0}.send{border-radius:12px;background:var(--primary-color);color:var(--text-primary-color,#fff);padding:8px 16px}.reply{font-size:14px;padding:10px 4px;line-height:1.5;white-space:pre-wrap}.subtle{color:var(--secondary-text-color);font-size:12px}.error{color:var(--error-color,#c44)}
      dialog{border:1px solid var(--divider-color);border-radius:20px;background:var(--card-background-color);color:var(--primary-text-color);width:min(480px,calc(100vw - 24px));padding:24px;max-height:90vh;overflow:auto}dialog::backdrop{background:#0006}dialog h2{margin:0 0 20px;font-size:22px}dialog label{display:block;margin:14px 0 6px;font-size:13px;color:var(--secondary-text-color)}dialog input,dialog textarea,dialog select{width:100%;padding:11px;border:1px solid var(--divider-color);border-radius:10px;background:var(--primary-background-color);color:inherit}dialog textarea{resize:vertical;min-height:78px}dialog input[type=checkbox]{width:auto;margin-right:8px}dialog .checklabel{display:flex;align-items:center;color:var(--primary-text-color)}dialog .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:24px}.danger{color:var(--error-color,#c44);margin-right:auto}.modalerror{font-size:13px;margin-top:12px;color:var(--error-color,#c44)}
      @media(max-width:700px){.app{padding:16px 12px 32px}.brand{width:48px;height:48px;border-radius:12px;flex-shrink:0}.header h1{font-size:24px}.day{min-height:82px;padding:4px}.event{padding:4px 3px;font-size:10px}.event time{display:block}.nav strong{min-width:150px}.header{gap:4px}.when{white-space:normal;max-width:90px}.weekdays span{padding:10px 5px}}
    </style><div class="app"></div><dialog></dialog>`;
    this.shadowRoot.addEventListener('click', event => this.click(event));
    this.shadowRoot.addEventListener('input', event => {if(event.target.id === 'compose') this.draft = event.target.value;});
    this.shadowRoot.addEventListener('change', event => {
      if(event.target.id === 'ai-provider')return this.aiSettings(event.target.value);
      if(event.target.id === 'priority-filter'){this.priority=event.target.value;this.render();}
      if(event.target.id === 'show-done'){this.showDone=event.target.checked;this.render();}
      if(event.target.id === 'task-all-day'){const time=this.shadowRoot.querySelector('dialog').querySelector('#task-time');if(time)time.disabled=event.target.checked;}
      if(event.target.id === 'tg-bot')this.telegramSettings(event.target.value);
      if(event.target.id === 'tg-chat'){
        const group=this.shadowRoot.querySelector('dialog').querySelector('#tg-sender');
        if(group)group.hidden=Number(event.target.value)>0;
      }
    });
    this.shadowRoot.addEventListener('keydown', event => {
      if(event.target.id==='compose' && event.key==='Enter' && !event.shiftKey && !event.isComposing){event.preventDefault();this.send();}
    });
  }
  set hass(value){this._hass=value;this.connect();}
  connectedCallback(){this._connected=true;this.connect();}
  disconnectedCallback(){this._connected=false;this._subscription?.();this._subscription=null;}
  async connect(){
    if(!this._hass || !this._connected || this._subscription || this._connecting) return;
    this._connecting=true;
    try{
      const unsubscribe=await this._hass.connection.subscribeMessage(data=>{if(!this.data)this.month=new Date(zoned(new Date().toISOString(),data.timezone).day+'T12:00:00');this.data=data;this.render();},{type:'personal_secretary/subscribe'});
      if(this._connected) this._subscription=unsubscribe; else unsubscribe();
    }catch(error){this.failure=error.message || 'Не получилось открыть дела';this.render();}
    finally{this._connecting=false;}
  }
  color(category){const i=this.data.categories.indexOf(category);return palette[(i<0?0:i)%palette.length];}
  important(task){return task.priority && task.priority!==this.data.priorities[0];}
  tasks(){return this.data.tasks.filter(t=>t.status!=='cancelled' && (this.showDone || t.status==='active') && (!this.category || t.category===this.category) && (!this.priority || t.priority===this.priority));}
  tags(task){return `<span class="tags">${task.classification_pending?'<span class="tag">Подбираю метки…</span>':`<span class="tag" style="--tag:${this.important(task)?'#d58443':'#8290a0'}">${this.important(task)?'★ ':''}${esc(task.priority)}</span>`}${task.category?`<span class="tag" style="--tag:${this.color(task.category)}">${esc(task.category)}</span>`:''}${task.status==='done' && task.completion_reason==='elapsed'?'<span class="tag">Завершено по времени</span>':''}${task.repeat_until_ack?`<span class="tag" style="--tag:#aa76c7">${task.reminder_ack_at?'✓ Ответ получен':'🔁 До ответа'}</span>`:''}</span>`;}
  item(task){const p=task.due_date?{day:task.due_date,time:'В течение дня'}:zoned(task.due_at,this.data.timezone);return `<div class="task"><button class="check ${task.status==='done'?'done':''}" data-action="toggle" data-id="${esc(task.id)}" aria-label="${task.status==='done'?'Вернуть в планы':'Отметить выполненным'}">${task.status==='done'?'✓':''}</button><button class="taskmain" data-action="edit" data-id="${esc(task.id)}"><span class="tasktitle ${task.status==='done'?'done':''}">${esc(task.title)}</span>${this.tags(task)}</button><span class="when">${p.day?`${esc(new Date(p.day+'T12:00:00').toLocaleDateString('ru-RU',{day:'numeric',month:'short'}))}<br>${p.time}`:'Без срока'}</span></div>`;}
  render(){
    const root=this.shadowRoot.querySelector('.app');
    if(!this.data){root.innerHTML=`<p>${esc(this.failure || 'Открываю твои дела…')}</p>`;return;}
    const focus=this.shadowRoot.activeElement, wasCompose=focus?.id==='compose', selection=wasCompose?[focus.selectionStart,focus.selectionEnd]:null;
    const tasks=this.tasks(), zone=this.data.timezone, today=zoned(new Date().toISOString(),zone).day;
    const monthTitle=this.month.toLocaleDateString('ru-RU',{month:'long',year:'numeric'});
    let content='';
    if(this.view==='calendar'){
      content=`<div class="board"><div class="weekdays">${['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].map(d=>`<span>${d}</span>`).join('')}</div><div class="grid">${monthDays(this.month).map(day=>{
        const key=localDay(day), events=tasks.filter(t=>onDay(t,key,zone)).sort((a,b)=>taskOrder(a,zone).localeCompare(taskOrder(b,zone)));
        return `<div class="day ${day.getMonth()!==this.month.getMonth()?'muted':''}"><button class="date ${key===today?'today':''} ${key===this.selectedDay?'picked':''}" data-action="day" data-day="${key}" aria-label="${esc(day.toLocaleDateString('ru-RU'))}">${day.getDate()}</button>${events.slice(0,3).map(t=>`<button class="event ${this.important(t)?'important':''} ${t.status==='done'?'completed':''}" style="--tag:${this.color(t.category)}" data-action="edit" data-id="${esc(t.id)}" title="${esc(t.title)}"><time>${t.due_date?'Весь день':zoned(t.due_at,zone).day===key?zoned(t.due_at,zone).time:'↳'}</time>${this.important(t)?'★ ':''}${esc(t.title)}</button>`).join('')}${events.length>3?`<button class="icon more" data-action="day" data-day="${key}">Ещё ${events.length-3}</button>`:''}</div>`;
      }).join('')}</div></div>`;
    }
    let shown=tasks.slice().sort((a,b)=>taskOrder(a,zone).localeCompare(taskOrder(b,zone)) || Number(this.important(b))-Number(this.important(a)));
    if(this.view==='calendar') shown=this.selectedDay?shown.filter(t=>onDay(t,this.selectedDay,zone)):shown.filter(t=>!taskDay(t,zone) || taskDay(t,zone)>=today || onDay(t,today,zone)).slice(0,12);
    const agendaTitle=this.selectedDay && this.view==='calendar'?new Date(this.selectedDay+'T12:00:00').toLocaleDateString('ru-RU',{day:'numeric',month:'long'}):this.view==='list'?'Все дела':'Ближайшие дела';
    const last=this.data.conversation.at(-1);
    root.innerHTML=`<div class="header"><ha-menu-button></ha-menu-button><img class="brand" src="/personal_secretary/icon.png?v=0.5.0" alt=""><div><h1>mindKeeper AI</h1><p>Планы, которые ты обсуждаешь с секретарём</p></div><span class="grow"></span><button class="icon" data-action="settings" aria-label="Настройки секретаря" title="Настройки секретаря">⚙</button></div>
      <div class="toolbar"><div class="nav"><button class="icon" data-action="prev" aria-label="Предыдущий месяц">‹</button><strong>${esc(monthTitle)}</strong><button class="icon" data-action="next" aria-label="Следующий месяц">›</button><button class="pill" data-action="today">Сегодня</button></div><span class="grow"></span><button class="pill ${this.view==='calendar'?'selected':''}" data-action="calendar">Календарь</button><button class="pill ${this.view==='list'?'selected':''}" data-action="list">Список</button></div>
      <div class="filters"><button class="pill ${!this.category?'selected':''}" data-action="category" data-value="">Все</button>${this.data.categories.map(c=>`<button class="pill ${this.category===c?'selected':''}" data-action="category" data-value="${esc(c)}">${esc(c)}</button>`).join('')}<span class="grow"></span><select id="priority-filter" aria-label="Важность"><option value="">Любая важность</option>${this.data.priorities.map(p=>`<option ${this.priority===p?'selected':''} value="${esc(p)}">${esc(p)}</option>`).join('')}</select></div>
      ${content}<section class="agenda"><div class="row"><h2>${esc(agendaTitle)}</h2><span class="grow"></span><button class="pill" data-action="new">＋ Добавить</button></div>${shown.length?shown.map(t=>this.item(t)).join(''):'<p class="empty">Здесь пока свободно. Расскажи секретарю, что планируешь.</p>'}</section>
      <div class="composer"><textarea id="compose" rows="1" aria-label="Сообщение секретарю" placeholder="Например: в пятницу в 16:00 приём у врача">${esc(this.draft)}</textarea><button class="send" data-action="send" aria-label="Отправить сообщение">↑</button></div>
      ${last?`<div class="reply">${esc(last.reply || (last.status==='error'?'Я сохранил сообщение и вернусь к нему чуть позже.':'Сейчас разберусь…'))}</div>`:''}
      <div class="row subtle"><label><input type="checkbox" id="show-done" ${this.showDone?'checked':''}> Показывать выполненные</label><span class="grow"></span>${this.data.pending?'Разбираюсь с новыми планами…':''}</div>${this.failure?`<p class="error">${esc(this.failure)}</p>`:''}`;
    root.querySelector('ha-menu-button').hass=this._hass;
    root.querySelector('ha-menu-button').narrow=this.narrow;
    if(wasCompose){const input=root.querySelector('#compose');input.focus();input.setSelectionRange(...selection);}
  }
  async call(type,data={}){return this._hass.callWS({type:'personal_secretary/'+type,...data});}
  async send(){
    const text=this.draft.trim();if(!text || this.sending)return;
    this.sending=true;
    try{await this.call('message',{text});this.draft='';this.failure='';this.render();}
    catch(error){this.failure=error.message;this.render();}finally{this.sending=false;}
  }
  async click(event){
    const button=event.target.closest('[data-action]');if(!button)return;
    const action=button.dataset.action;
    try{
      if(action==='prev'||action==='next'){this.month=new Date(this.month.getFullYear(),this.month.getMonth()+(action==='next'?1:-1),1);this.selectedDay=null;}
      if(action==='today'){this.month=new Date(zoned(new Date().toISOString(),this.data.timezone).day+'T12:00:00');this.selectedDay=null;}
      if(action==='calendar'||action==='list'){this.view=action;this.selectedDay=null;}
      if(action==='category')this.category=button.dataset.value;
      if(action==='day')this.selectedDay=this.selectedDay===button.dataset.day?null:button.dataset.day;
      if(action==='send')return this.send();
      if(action==='edit'||action==='new')return this.edit(action==='new'?null:this.data.tasks.find(t=>t.id===button.dataset.id));
      if(action==='settings')return this.settings();
      if(action==='ai')return this.aiSettings();
      if(action==='save-ai')return this.saveAI();
      if(action==='test-ai')return this.testAI();
      if(action==='sync-google')return this.syncGoogle();
      if(action==='telegram')return this.telegramSettings();
      if(action==='save-telegram')return this.saveTelegram();
      if(action==='close')return this.shadowRoot.querySelector('dialog').close();
      if(action==='toggle'){const task=this.data.tasks.find(t=>t.id===button.dataset.id);await this.call('change',{operation:{action:task.status==='done'?'reopen':'done',task_id:task.id,revision:task.revision}});}
      if(action==='save-task')return this.saveTask();
      if(action==='save-settings')return this.saveSettings();
      if(action==='refresh-agenda')return this.updateAgenda();
      if(action==='cancel-task'){await this.call('change',{operation:{action:'cancel',task_id:this.editing.id,revision:this.editing.revision}});return this.shadowRoot.querySelector('dialog').close();}
      this.render();
    }catch(error){this.failure=error.message;this.render();}
  }
  open(content){const dialog=this.shadowRoot.querySelector('dialog');dialog.innerHTML=content;if(!dialog.open)dialog.showModal();}
  edit(task){
    this.editing=task?{...task}:null;
    const p=task?.due_date?{day:task.due_date,time:''}:zoned(task?.due_at,this.data.timezone);
    if(!task)p.day=this.selectedDay || zoned(new Date().toISOString(),this.data.timezone).day;
    const choices=(values,current)=>`<option value="">Пусть выберет секретарь</option>${[...new Set([...values,...(current?[current]:[])])].map(v=>`<option value="${esc(v)}" ${v===current?'selected':''}>${esc(v)}</option>`).join('')}`;
    this.open(`<h2>${task?'Дело':'Что запланируем?'}</h2><label for="task-title">Что нужно сделать</label><input id="task-title" maxlength="300" value="${esc(task?.title)}"><label for="task-date">День</label><input id="task-date" type="date" value="${p.day}"><label class="checklabel"><input id="task-all-day" type="checkbox" ${!task?.due_at?'checked':''}>В течение дня, без точного времени</label><label for="task-time">Время · ${esc(this.data.timezone)}</label><input id="task-time" type="time" value="${p.time}" ${!task?.due_at?'disabled':''}><label for="task-category">Категория</label><select id="task-category">${choices(this.data.categories,task?.category)}</select><label for="task-priority">Важность</label><select id="task-priority">${choices(this.data.priorities,task?.priority)}</select><label for="task-notes">Подробности</label><textarea id="task-notes">${esc(task?.notes)}</textarea><label class="checklabel"><input id="task-repeat" type="checkbox" ${task?.repeat_until_ack?'checked':''}>🔁 Напоминать до ответа</label><p class="subtle">Первое напоминание — за час до начала. Для дела на день — по настройке ниже. Затем каждые ${this.data.repeat_interval_minutes || 10} минут, пока не отвечу в чате. Ответ на напоминание завершает дело. Без этого режима дело завершится автоматически после окончания события, а дело на день — в конце дня.</p><div class="modalerror"></div><div class="actions">${task?'<button class="pill danger" data-action="cancel-task">Убрать из планов</button>':''}<button class="pill" data-action="close">Закрыть</button><button class="send" data-action="save-task">Сохранить</button></div>`);
  }
  async saveTask(){
    const dialog=this.shadowRoot.querySelector('dialog'), value=id=>dialog.querySelector('#'+id).value;
    const day=value('task-date'), allDay=dialog.querySelector('#task-all-day').checked;
    if(day && !allDay && !value('task-time')){dialog.querySelector('.modalerror').textContent='Укажи время или выбери «В течение дня»';return;}
    const op={action:this.editing?'update':'create',title:value('task-title'),notes:value('task-notes'),due_at:day&&!allDay?day+'T'+value('task-time'):null,due_date:day&&allDay?day:null,category:value('task-category')||null,priority:value('task-priority')||null,clear_dates:!value('task-date')};
    op.classify_fields=['category','priority'].filter(k=>!op[k]);
    op.repeat_until_ack=dialog.querySelector('#task-repeat').checked;
    if(this.editing?.status==='done'){const prior=zoned(this.editing.due_at,this.data.timezone);if((this.editing.due_date || prior.day)!==day || Boolean(this.editing.due_at)!==Boolean(day&&!allDay) || (day&&!allDay && prior.time!==value('task-time')))op.action='reopen';}
    if(this.editing){op.task_id=this.editing.id;op.revision=this.editing.revision;}
    try{await this.call('change',{operation:op});dialog.close();}catch(error){dialog.querySelector('.modalerror').textContent=error.message;}
  }
  settings(){
    this.open(`<h2>mindKeeper AI · настройки</h2><p class="subtle">Секретарь сам подбирает метки по смыслу дела. По одному названию в строке.</p><label for="categories">Категории</label><textarea id="categories" rows="6">${esc(this.data.categories.join('\n'))}</textarea><label for="priorities">Уровни важности</label><textarea id="priorities" rows="3">${esc(this.data.priorities.join('\n'))}</textarea><p class="subtle">Первый уровень — обычные дела. Остальные — повышенная важность.</p><label for="repeat-interval">Повтор напоминаний, минут</label><input id="repeat-interval" type="number" min="1" max="180" step="1" value="${this.data.repeat_interval_minutes || 10}"><p class="subtle">Обычное напоминание — за час до начала. Этот интервал отсчитывается после первого уведомления и действует только с меткой «Напоминать до ответа».</p><label for="day-mode">Первое напоминание о деле без времени</label><select id="day-mode"><option value="after_hour" ${this.data.all_day_reminder_mode!=='day_time'?'selected':''}>Через час после добавления сегодня</option><option value="day_time" ${this.data.all_day_reminder_mode==='day_time'?'selected':''}>В заданное время выбранного дня</option></select><label for="day-clock">Время напоминания о деле на день</label><input id="day-clock" type="time" value="${esc(this.data.all_day_reminder_time || '09:00')}"><p class="subtle">Для будущих дней используется это время в часовом поясе ${esc(this.data.timezone)}. Если выбранное время сегодня уже прошло — напомню сразу.</p><label class="checklabel"><input id="agenda-enabled" type="checkbox" ${this.data.agenda_enabled!==false?'checked':''}>Сообщение «Дела на сегодня» в Telegram</label><p class="subtle">Одно сообщение с делами и временем. Закрепи его в чате — секретарь будет обновлять этот же список.</p><button class="pill" data-action="refresh-agenda">${this.data.agenda_status==='uncertain'?'Создать сообщение снова':'Обновить сообщение'}</button>${this.aiSummary()}${this.googleSettings()}<div class="row" style="margin-top:20px"><div class="grow"><strong>Telegram</strong><p class="subtle">${esc(this.data.telegram?.bot_label || '')}<br>${esc(this.data.telegram?.chat_label || '')}</p></div><button class="pill" data-action="telegram">Настроить</button></div><div class="modalerror"></div><div class="actions"><button class="pill" data-action="close">Закрыть</button><button class="send" data-action="save-settings">Сохранить</button></div>`);
  }
  aiSummary(){
    const ai=this.data.ai, active=ai?.providers.find(p=>p.value===ai.provider);
    return `<div class="row" style="margin-top:20px"><div class="grow"><strong>AI · ${esc(active?.label || 'Codex')}</strong><p class="subtle">Выбор сервиса, модели и подключения</p></div><button class="pill" data-action="ai">Настроить AI</button></div>`;
  }
  aiSettings(provider){
    const ai=this.data.ai;
    if(!ai){this.failure='Обнови страницу, чтобы загрузить настройки AI';this.render();return;}
    provider=provider || ai.provider;
    const spec=ai.providers.find(p=>p.value===provider);
    if(!spec)return;
    const config=ai.configurations[provider];
    this.open(`<h2>AI для mindKeeper</h2><label for="ai-provider">Кто будет разбирать планы</label><select id="ai-provider">${ai.providers.map(p=>`<option value="${esc(p.value)}" ${p.value===provider?'selected':''}>${esc(p.label)}</option>`).join('')}</select><p class="subtle">${esc(spec.hint)}</p><label for="ai-model">Модель${provider==='codex'?' · default — выбор Codex':''}</label><input id="ai-model" value="${esc(config.model)}" maxlength="300" autocomplete="off">${provider!=='codex'?`<label for="ai-key">API-ключ${spec.key_required?'':' · необязательно'}</label><input id="ai-key" type="password" name="mindkeeper-${esc(provider)}-key" autocomplete="new-password" placeholder="${config.key_configured?'Ключ сохранён · оставь пустым, чтобы сохранить':'Вставь ключ из кабинета провайдера'}"><p class="subtle">Сохранённый ключ не показывается. Ключи разных сервисов хранятся отдельно.</p>${config.key_configured?'<label class="checklabel"><input id="ai-clear-key" type="checkbox">Удалить сохранённый ключ</label>':''}`:'<p class="subtle">Вход в ChatGPT: <a href="/config/integrations/integration/personal_secretary">Устройства и службы → mindKeeper AI → Настроить → Войти в ChatGPT</a>. Для этого способа Codex должен быть установлен на сервере HA.</p>'}${spec.custom_url?`<label for="ai-base-url">Базовый адрес API</label><input id="ai-base-url" value="${esc(config.base_url)}" placeholder="http://адрес-сервера:11434/v1" autocomplete="off">`:''}${spec.folder_required?`<label for="ai-folder">ID каталога Яндекса</label><input id="ai-folder" value="${esc(config.folder_id)}" autocomplete="off">`:''}<p class="subtle">Выбранный сервис получает сообщения и контекст дел. Автоматического переключения на другой AI нет. Проверка отправляет только тестовую фразу, без твоих дел.</p>${spec.url?`<p><a href="${esc(spec.url)}" target="_blank" rel="noopener">Документация и условия сервиса</a></p>`:''}<div class="ai-result subtle" role="status"></div><div class="actions"><button class="pill" data-action="close">Закрыть</button><button class="pill" data-action="test-ai">Проверить подключение</button><button class="send" data-action="save-ai">Сохранить AI</button></div>`);
  }
  aiSelection(){
    const dialog=this.shadowRoot.querySelector('dialog');
    const get=id=>dialog.querySelector('#'+id);
    const value={provider:get('ai-provider').value,model:get('ai-model').value};
    for(const [field,id] of [['api_key','ai-key'],['base_url','ai-base-url'],['folder_id','ai-folder']])if(get(id))value[field]=get(id).value;
    if(get('ai-clear-key'))value.clear_key=get('ai-clear-key').checked;
    return value;
  }
  async saveAI(){
    if(this.aiActionPending)return;
    this.aiActionPending=true;
    const dialog=this.shadowRoot.querySelector('dialog');
    try{this.data.ai=await this.call('ai',this.aiSelection());this.aiSettings(this.data.ai.provider);this.shadowRoot.querySelector('dialog').querySelector('.ai-result').textContent='AI выбран. Дела и настройки напоминаний сохранены.';}
    catch(error){dialog.querySelector('.ai-result').textContent=error.message;}
    finally{this.aiActionPending=false;}
  }
  async testAI(){
    if(this.aiActionPending)return;
    this.aiActionPending=true;
    const target=this.shadowRoot.querySelector('dialog').querySelector('.ai-result');
    target.textContent='Проверяю подключение…';
    try{const result=await this.call('ai_test',this.aiSelection());target.textContent=result.message;}
    catch(error){target.textContent=error.message;}
    finally{this.aiActionPending=false;}
  }
  googleSettings(){
    const google=this.data.google || {calendars:[]};
    const states={disabled:'Выключена',waiting:'Ожидаю первого обмена',ready:'Синхронизировано',error:'Требует внимания'};
    return `<h3>Google Calendar</h3><label for="google-calendar">Календарь для дел</label><select id="google-calendar"><option value="">Синхронизация выключена</option>${google.calendars.map(c=>`<option value="${esc(c.value)}" ${c.value===google.calendar?'selected':''}>${esc(c.label)}</option>`).join('')}</select><label for="google-mode">Направление обмена</label><select id="google-mode"><option value="bidirectional" ${google.mode!=='export'?'selected':''}>В обе стороны</option><option value="export" ${google.mode==='export'?'selected':''}>Только дела → Google</option></select><p class="subtle">В обе стороны: события выбранного календаря становятся делами, изменения названия, описания и дат передаются обратно. Удаление события отменяет дело; выполненное дело остаётся в Google с отметкой ✅. Лучше выбрать отдельный календарь для дел. Обмен — каждые 5 минут и после изменений в HA.</p><p class="subtle">${esc(states[google.status] || '')}${google.last_success?' · '+esc(new Date(google.last_success).toLocaleString('ru-RU',{timeZone:this.data.timezone})):''}${google.conflicts?' · При одновременных правках выбраны значения из Google: '+google.conflicts:''}</p>${google.error?`<p class="error">${esc(google.error)}</p>`:''}<p><a href="https://my.home-assistant.io/redirect/config_flow_start/?domain=google" target="_blank" rel="noopener">Подключить штатный Google Calendar</a> · <a href="https://www.home-assistant.io/integrations/google/" target="_blank" rel="noopener">Инструкция</a></p><p class="subtle">После подключения обнови страницу, выбери календарь и сохрани настройки. Доступом к аккаунту управляет штатная интеграция Google.</p><button class="pill" data-action="sync-google">Синхронизировать сейчас</button>`;
  }
  async syncGoogle(){
    const dialog=this.shadowRoot.querySelector('dialog');
    try{await this.call('google_sync');dialog.querySelector('.modalerror').textContent='Синхронизация выполнена';}
    catch(error){dialog.querySelector('.modalerror').textContent=error.message;}
  }
  telegramSettings(botId){
    const connection=this.data.telegram;
    if(!connection){this.failure='Обнови страницу, чтобы загрузить настройки Telegram';this.render();return;}
    botId=botId || connection.route.bot_entry_id;
    const chats=connection.chats.filter(c=>c.bot_entry_id===botId);
    const current=chats.find(c=>c.chat_id===connection.route.chat_id) || chats[0];
    this.open(`<h2>Telegram</h2><label for="tg-bot">Через какого бота общаться</label><select id="tg-bot">${connection.bots.map(b=>`<option value="${esc(b.value)}" ${b.value===botId?'selected':''}>${esc(b.label)}</option>`).join('')}</select><label for="tg-chat">Кому писать</label><select id="tg-chat">${chats.length?chats.map(c=>`<option value="${c.chat_id}" ${c.chat_id===current.chat_id?'selected':''}>${esc(c.label)}</option>`).join(''):'<option value="">Сначала добавьте чат в Telegram Bot</option>'}</select><div id="tg-sender" ${!current || current.chat_id>0?'hidden':''}><label for="tg-user">Кого слушать в группе · Telegram ID</label><input id="tg-user" type="text" inputmode="numeric" value="${esc(connection.route.user_id)}"><p class="subtle">Другие участники не смогут менять твои планы. В группе у бота должен быть доступ к обычным сообщениям.</p></div><p class="subtle">В личном чате собеседник определяется автоматически. Задачи сохранятся при смене бота или чата.</p><p class="subtle"><a href="/config/integrations/integration/telegram_bot" style="color:var(--primary-color)">Добавить бота или разрешённый чат в Home Assistant</a></p><div class="modalerror"></div><div class="actions"><button class="pill" data-action="close">Закрыть</button><button class="send" data-action="save-telegram">Сохранить</button></div>`);
  }
  async saveTelegram(){
    const dialog=this.shadowRoot.querySelector('dialog');
    const route={bot_entry_id:dialog.querySelector('#tg-bot').value,chat_id:dialog.querySelector('#tg-chat').value,user_id:dialog.querySelector('#tg-user').value};
    try{await this.call('telegram',{route});dialog.close();}catch(error){dialog.querySelector('.modalerror').textContent=error.message;}
  }
  async updateAgenda(){
    const dialog=this.shadowRoot.querySelector('dialog');
    try{await this.call('agenda',{recreate:this.data.agenda_status==='uncertain'});dialog.querySelector('.modalerror').textContent='Сообщение обновлено';}
    catch(error){dialog.querySelector('.modalerror').textContent=error.message;}
  }
  async saveSettings(){
    const dialog=this.shadowRoot.querySelector('dialog'), lines=id=>dialog.querySelector('#'+id).value.split('\n').map(s=>s.trim()).filter(Boolean);
    try{await this.call('settings',{categories:lines('categories'),priorities:lines('priorities'),repeat_interval_minutes:Number(dialog.querySelector('#repeat-interval').value),agenda_enabled:dialog.querySelector('#agenda-enabled').checked,all_day_reminder_mode:dialog.querySelector('#day-mode').value,all_day_reminder_time:dialog.querySelector('#day-clock').value,google_calendar:dialog.querySelector('#google-calendar').value,google_sync_mode:dialog.querySelector('#google-mode').value});dialog.close();}catch(error){dialog.querySelector('.modalerror').textContent=error.message;}
  }
}
if(!customElements.get('personal-secretary-panel'))customElements.define('personal-secretary-panel', PersonalSecretaryPanel);
