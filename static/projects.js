/*
 * Projects for Highlights Video (episodic) and Artwork Animation (schedule) -- the same screens as the
 * Vertical Shorts projects (templates/index.html, "---- Projects ----"):
 * a list of project cards, a form for a project's details, and an open
 * project with its header. Shorts keeps its own copy of this code; this one
 * is written once and used by both tabs, each handing in what differs.
 *
 *   var ctl = PrismProjects.create({
 *     kind: 'episodic',            // the URL prefix: /api/<kind>/projects
 *     prefix: 'ep',                // prefix for the element ids this makes
 *     mount: 'ep-projects',        // id of the empty element the list view is drawn in
 *     work: 'ep-work',             // id of the tab's existing screens, shown only inside a project
 *     intro: 'ep-intro',           // optional: an element hidden while a project is open
 *     noun: ['plug', 'plugs'],
 *     onOpen: function(project, fresh){},   // a project was opened (fresh: just created)
 *     onClose: function(){},
 *     loadUnfiled: function(ctl, host, wrap){}   // optional: fills "Not in a project"
 *   })
 *
 * The open project's id is kept in window.PRISM_PROJECT[kind], which the
 * tabs read when they start a job or list what is filed.
 *
 * Everything shown that came from a person or a file is escaped here.
 */
(function(){
  var STATUS = {new: 'New', in_progress: 'In Progress', ready_review: 'Ready For Review',
    review_in_progress: 'Review In Progress', ready_export: 'Ready To Export', complete: 'Complete'}

  window.PRISM_PROJECT = window.PRISM_PROJECT || {}

  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]
    })
  }

  function dateText(iso){
    if(!iso) return ''
    var d = new Date(iso + 'T00:00:00')
    return isNaN(d) ? iso : d.toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'})
  }

  function statusPill(p){
    var s = STATUS[p.status] ? p.status : 'new'
    return '<span class="sh-pill ' + s + '">' + STATUS[s] + '</span>'
  }

  function dueText(p){
    if(!p.deadline) return ''
    var d = new Date(p.deadline)
    if(isNaN(d)) return ''
    var late = d < new Date() && p.status !== 'complete'
    var t = d.toLocaleString(undefined, {month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit'})
    return '<span' + (late ? ' class="sh-pill overdue"' : '') + '>due ' + esc(t) + (late ? ' (overdue)' : '') + '</span>'
  }

  function count(p, noun){
    return p.count ? p.count + ' ' + (p.count === 1 ? noun[0] : noun[1]) : 'No ' + noun[1] + ' yet'
  }

  function create(o){
    var kind = o.kind, P = o.prefix + '-pj', noun = o.noun || ['plug', 'plugs']
    var S = {projects: null, groups: [], isAdmin: false, open: null, editing: null, timer: null, assignees: {}}
    var root = document.getElementById(o.mount)
    var work = document.getElementById(o.work)
    var intro = o.intro ? document.getElementById(o.intro) : null
    var KEY = 'prism_' + kind + '_project', FKEY = 'prism_' + kind + '_proj_filter'
    if(!root) return null

    function el(id){ return document.getElementById(P + '-' + id) }

    root.innerHTML =
      '<div id="' + P + '-list">' +
        '<div class="sh-bar"><b>Projects</b><span style="flex:1"></span>' +
          '<button type="button" class="btn" id="' + P + '-new">New project</button></div>' +
        '<div id="' + P + '-form" class="card" style="display:none">' +
          '<b id="' + P + '-heading">New project</b>' +
          '<div class="sh-proj-form" style="margin-top:10px">' +
            '<div><label for="' + P + '-title">Programme title:</label>' +
              '<input type="text" id="' + P + '-title" class="input-box" maxlength="120" placeholder="e.g. Tadhana"></div>' +
            '<div><label for="' + P + '-episode">Program Code:</label>' +
              '<input type="text" id="' + P + '-episode" class="input-box" maxlength="40" placeholder="e.g. TDH-101"></div>' +
            '<div><label for="' + P + '-eptitle">Title:</label>' +
              '<input type="text" id="' + P + '-eptitle" class="input-box" maxlength="120" placeholder="e.g. Ang Pagbabalik"></div>' +
            '<div><label for="' + P + '-artists">Artist/s:</label>' +
              '<input type="text" id="' + P + '-artists" class="input-box" maxlength="300" placeholder="e.g. Juan Dela Cruz, Maria Santos"></div>' +
            '<div><label for="' + P + '-air">Air date:</label>' +
              '<input type="date" id="' + P + '-air" class="input-box"></div>' +
            '<div><label for="' + P + '-status">Status:</label>' +
              '<select id="' + P + '-status" class="input-box">' +
                Object.keys(STATUS).map(function(k){ return '<option value="' + k + '">' + STATUS[k] + '</option>' }).join('') +
              '</select></div>' +
            '<div><label for="' + P + '-assign">Assigned to:</label>' +
              '<select id="' + P + '-assign" class="input-box"><option value="">Unassigned</option></select></div>' +
            '<div id="' + P + '-group-wrap"><label>Group:</label>' +
              '<div id="' + P + '-group" style="font-size:13px;padding:6px 0" title="A project belongs to the groups of whoever created it, and follows them if they change group"></div></div>' +
            '<div><label for="' + P + '-hasdue" style="display:flex;gap:8px;align-items:center;cursor:pointer">' +
              '<input type="checkbox" id="' + P + '-hasdue"> Has a Deadline</label>' +
              '<div id="' + P + '-due" style="display:none;margin-top:6px">' +
                '<label for="' + P + '-duedate">Due Date:</label><input type="date" id="' + P + '-duedate" class="input-box">' +
                '<label for="' + P + '-duetime" style="margin-top:6px">Due Time:</label>' +
                '<input type="time" id="' + P + '-duetime" class="input-box" value="17:00"></div></div>' +
            '<div><label for="' + P + '-thumb">Thumbnail (JPEG, PNG or WebP):</label>' +
              '<input type="file" id="' + P + '-thumb" accept="image/jpeg,image/png,image/webp">' +
              '<label id="' + P + '-remove-wrap" style="display:none;font-size:12px;margin-top:4px">' +
                '<input type="checkbox" id="' + P + '-remove"> Remove the current thumbnail</label></div>' +
          '</div>' +
          '<label for="' + P + '-desc" style="margin-top:10px">Description:</label>' +
          '<textarea id="' + P + '-desc" class="input-box" maxlength="1000" rows="3" ' +
            'placeholder="What it is about, who it is for, anything the next person should know."></textarea>' +
          '<p class="sh-note" style="margin-top:6px">Only the title is required.</p>' +
          '<div style="display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap">' +
            '<button type="button" class="btn" id="' + P + '-save">Create project</button>' +
            '<button type="button" class="btn-sm" id="' + P + '-cancel">Cancel</button>' +
            '<span id="' + P + '-note" style="font-size:12px"></span></div>' +
        '</div>' +
        '<div style="display:flex;gap:8px;align-items:center;margin:0 0 8px;font-size:12px">' +
          '<label for="' + P + '-filter" style="margin:0">Show</label>' +
          '<select id="' + P + '-filter" style="width:auto;margin:0;padding:3px 26px 3px 8px;font-size:12px">' +
            '<option value="all">All projects</option>' +
            Object.keys(STATUS).map(function(k){ return '<option value="' + k + '">' + STATUS[k] + '</option>' }).join('') +
          '</select></div>' +
        '<div id="' + P + '-grid"></div>' +
        '<div id="' + P + '-unfiled-wrap" style="display:none;margin-top:22px">' +
          '<div class="sh-bar"><b>Not in a project</b>' +
            '<span style="font-size:12px;opacity:.75">Made before projects. Use <b>Move to</b> on each to file it.</span></div>' +
          '<div id="' + P + '-unfiled"></div></div>' +
      '</div>' +
      '<div id="' + P + '-open" style="display:none">' +
        '<button type="button" class="btn-sm" id="' + P + '-back">&larr; All projects</button>' +
        '<div id="' + P + '-head"></div>' +
      '</div>'

    // ---- one project, as a line, a picture, and what is running in it ----
    function line(p){
      var bits = [statusPill(p)]
      if(p.episode || p.episode_title) bits.push(esc([p.episode, p.episode_title].filter(Boolean).join(' \u2013 ')))
      if(p.artists) bits.push(esc(p.artists))
      if(p.air_date) bits.push('airs ' + esc(dateText(p.air_date)))
      if(p.assigned_to) bits.push('assigned to ' + esc(p.assigned_to))
      if(p.group_name || S.isAdmin) bits.push(p.group_name ? 'group ' + esc(p.group_name) : 'no group')
      var due = dueText(p)
      if(due) bits.push(due)
      return bits.join(' &middot; ')
    }

    function thumb(p){
      var initials = (p.title || '?').split(/\s+/).slice(0, 2).map(function(w){ return w.charAt(0) }).join('').toUpperCase()
      return '<div class="sh-proj-thumb"' +
        (p.thumb_url ? ' style="background-image:url(\'' + esc(encodeURI(p.thumb_url)) + '\')"' : '') + '>' +
        (p.thumb_url ? '' : esc(initials)) + '</div>'
    }

    function jobHtml(jobs){
      if(!jobs || !jobs.length) return ''
      var j = jobs[0], waiting = j.state === 'waiting'
      return '<div class="sh-pjob' + (waiting ? ' waiting' : '') + '" data-pjob>' +
        '<div class="sh-pjob-top"><span>' + esc(j.label) + (jobs.length > 1 ? ' &middot; +' + (jobs.length - 1) + ' more' : '') + '</span>' +
        '<span>' + (waiting ? 'Waiting' : j.percent + '%') + '</span></div>' +
        '<div class="sh-pjob-bar"><div style="width:' + (waiting ? 4 : j.percent) + '%"></div></div>' +
        '<div class="sh-pjob-step">' + esc(waiting ? 'Waiting for a free slot' : (j.step || 'Working\u2026')) + '</div></div>'
    }

    function find(pid){
      return (S.projects || []).filter(function(x){ return x.project_id === pid })[0]
    }

    // ---- what is running: cards are updated where they stand ----
    function stopTimer(){ if(S.timer){ clearInterval(S.timer); S.timer = null } }

    async function jobsTick(){
      var grid = el('grid')
      if(!grid) return
      var d
      try{ d = await (await fetch('/api/' + kind + '/projects/activity')).json() }catch(e){ return }
      if(!d || !d.ok) return
      var finished = false
      ;(S.projects || []).forEach(function(p){
        var now = d.jobs[p.project_id] || []
        if((p.jobs || []).length && !now.length) finished = true
        p.jobs = now
        var card = grid.querySelector('[data-project="' + p.project_id + '"]')
        if(!card) return
        var cur = card.querySelector('[data-pjob]'), body = card.querySelector('.sh-proj-body')
        if(cur) cur.remove()
        if(body && now.length) body.insertAdjacentHTML('beforeend', jobHtml(now))
      })
      // A project that had a job and has none now has new counts: only a reload brings them, and not while one
      // is open (that would redraw the screen someone is working on).
      if(finished && !S.open) load()
      else if(!Object.keys(d.jobs).length) stopTimer()
    }

    function poll(){
      var any = (S.projects || []).some(function(p){ return p.jobs && p.jobs.length })
      if(!any){ stopTimer(); return }
      if(!S.timer) S.timer = setInterval(jobsTick, 3000)
    }

    // ---- the list ----
    async function load(){
      var grid = el('grid')
      if(!grid) return
      var first = S.projects === null
      if(first){
        try{
          var sv = localStorage.getItem(FKEY)
          if(sv){ el('filter').value = sv; if(el('filter').value !== sv) el('filter').value = 'all' }
        }catch(e){}
        grid.innerHTML = '<div class="no-data">Loading&hellip;</div>'
      }
      try{
        var d = await (await fetch('/api/' + kind + '/projects')).json()
        if(!d.ok) throw new Error(d.error || 'Not allowed')
        S.projects = d.items
        S.isAdmin = !!d.is_admin
      }catch(e){
        S.projects = S.projects || []
        grid.innerHTML = '<div class="no-data"><span style="color:var(--amber)">Could not load the projects: ' + esc(String(e)) + '</span></div>'
        return
      }
      var flt = el('filter').value || 'all'
      var shown = S.projects.filter(function(p){ return flt === 'all' || (p.status || 'new') === flt })
      grid.className = shown.length ? 'sh-proj-grid' : ''
      grid.innerHTML = shown.length ? '' : (S.projects.length
        ? '<div class="no-data">No ' + esc(STATUS[flt] || flt) + ' projects. Change <b>Show</b> above to see the others.</div>'
        : '<div class="no-data">No projects yet. Choose <b>New project</b> to create the first one: ' + esc(noun[1]) + ' are made inside a project.</div>')
      shown.forEach(function(p){
        var card = document.createElement('button')
        card.type = 'button'
        card.className = 'sh-proj'
        card.setAttribute('data-project', p.project_id)
        var l = line(p)
        card.innerHTML = thumb(p) +
          '<div class="sh-proj-body"><b title="' + esc(p.title || '') + '">' + esc(p.title || 'Untitled') + '</b>' +
            (l ? '<div style="opacity:.85">' + l + '</div>' : '') +
            (p.description ? '<div class="sh-proj-desc">' + esc(p.description) + '</div>' : '') +
            '<div style="opacity:.6;margin-top:6px">' + esc(count(p, noun)) + '</div>' + jobHtml(p.jobs) + '</div>'
        card.addEventListener('click', function(){ openProject(p.project_id) })
        grid.appendChild(card)
      })
      // A reload comes back to the project that was open.
      if(first && !S.open){
        try{ S.open = localStorage.getItem(KEY) || null }catch(e){}
      }
      if(S.open){
        if(find(S.open)) openProject(S.open, true)
        else closeProject()
      }
      loadUnfiled()
      poll()
    }

    function loadUnfiled(){
      var wrap = el('unfiled-wrap'), host = el('unfiled')
      if(!wrap || !host || !o.loadUnfiled) return
      o.loadUnfiled(ctl, host, wrap)
    }

    // A "Move to" control for a plug that is in no project (or in the wrong one): calls move(projectId).
    function moveSelect(move){
      var sel = document.createElement('select')
      sel.style.cssText = 'width:auto;margin:0;padding:3px 26px 3px 8px;font-size:12px'
      sel.innerHTML = '<option value="">Move to\u2026</option>' + (S.projects || []).map(function(p){
        return '<option value="' + esc(p.project_id) + '">' + esc(p.name || p.title) + '</option>' }).join('')
      sel.addEventListener('change', function(){ if(sel.value) move(sel.value) })
      return sel
    }

    function filterChanged(){
      try{ localStorage.setItem(FKEY, el('filter').value) }catch(e){}
      load()
    }

    // ---- the form ----
    async function loadAssignees(current, pid){
      var sel = el('assign'), key = pid || ''
      var names = S.assignees[key]
      if(!names){
        try{
          var d = await (await fetch('/api/' + kind + '/assignees' + (pid ? '?project_id=' + encodeURIComponent(pid) : ''))).json()
          names = S.assignees[key] = d.ok ? d.items : []
        }catch(e){ names = [] }
      }
      var all = names.slice()
      if(current && all.indexOf(current) < 0) all.push(current)
      sel.innerHTML = '<option value="">Unassigned</option>' + all.map(function(n){
        return '<option value="' + esc(n) + '">' + esc(n) + '</option>' }).join('')
      sel.value = current || ''
    }

    function dueToggle(){ el('due').style.display = el('hasdue').checked ? 'block' : 'none' }

    function form(p, hide){
      var f = el('form')
      if(hide){
        var back = S.editing
        f.style.display = 'none'; S.editing = null
        if(back && S.open === back) openProject(back, true)
        return
      }
      S.editing = p ? p.project_id : null
      el('heading').textContent = p ? 'Edit project' : 'New project'
      el('save').textContent = p ? 'Save changes' : 'Create project'
      el('title').value = p ? (p.title || '') : ''
      el('episode').value = p ? (p.episode || '') : ''
      el('eptitle').value = p ? (p.episode_title || '') : ''
      el('artists').value = p ? (p.artists || '') : ''
      el('air').value = p ? (p.air_date || '') : ''
      el('status').value = p ? (p.status || 'new') : 'new'
      loadAssignees(p ? p.assigned_to : '', p ? p.project_id : '')
      // Nobody picks the group: a project belongs to its creator's current groups.
      el('group').textContent = p ? ((p.group_name || 'no group') + ' (its creator\u2019s)') : 'Your group'
      var dl = p && p.deadline ? p.deadline.split('T') : null
      el('hasdue').checked = !!dl
      el('duedate').value = dl ? dl[0] : ''
      el('duetime').value = dl && dl[1] ? dl[1] : '17:00'
      dueToggle()
      el('desc').value = p ? (p.description || '') : ''
      el('thumb').value = ''
      el('remove').checked = false
      el('remove-wrap').style.display = p && p.has_thumb ? 'block' : 'none'
      el('note').textContent = ''
      // The form lives in the list view; an open project is closed to show it.
      showOpen(false)
      f.style.display = 'block'
      el('title').focus()
    }

    async function save(){
      var note = el('note'), btn = el('save')
      var title = el('title').value.trim()
      note.style.color = 'var(--tally)'
      if(!title){ note.textContent = 'Give the project a programme title.'; el('title').focus(); return }
      var fd = new FormData()
      fd.append('title', title)
      fd.append('episode', el('episode').value)
      fd.append('episode_title', el('eptitle').value)
      fd.append('artists', el('artists').value)
      fd.append('air_date', el('air').value)
      fd.append('status', el('status').value)
      fd.append('assigned_to', el('assign').value)
      if(el('hasdue').checked){
        if(!el('duedate').value){ note.textContent = 'Choose the due date, or turn off Has a Deadline.'; el('duedate').focus(); return }
        fd.append('has_deadline', '1')
        fd.append('due_date', el('duedate').value)
        fd.append('due_time', el('duetime').value)
      }else fd.append('has_deadline', '0')
      fd.append('description', el('desc').value)
      var f = el('thumb')
      if(f.files && f.files[0]) fd.append('thumbnail', f.files[0])
      if(el('remove').checked) fd.append('remove_thumbnail', '1')
      btn.disabled = true
      note.style.color = ''; note.textContent = 'Saving\u2026'
      try{
        var r = await (await fetch('/api/' + kind + '/projects' + (S.editing ? '/' + S.editing : ''), {method: 'POST', body: fd})).json()
        if(!r.ok){ note.style.color = 'var(--tally)'; note.textContent = r.error || 'Could not save the project.'; btn.disabled = false; return }
        var edited = !!S.editing
        form(null, true)
        S.open = r.project.project_id
        S.fresh = !edited
        await load()
      }catch(e){ note.style.color = 'var(--tally)'; note.textContent = 'Could not save the project: ' + e }
      btn.disabled = false
    }

    // ---- one project ----
    function showOpen(on){
      el('open').style.display = on ? 'block' : 'none'
      el('list').style.display = on ? 'none' : 'block'
      if(work) work.style.display = on ? '' : 'none'
      if(intro) intro.style.display = on ? 'none' : ''
    }

    function openProject(pid, refresh){
      var p = find(pid)
      if(!p) return
      S.open = pid
      window.PRISM_PROJECT[kind] = pid
      try{ localStorage.setItem(KEY, pid) }catch(e){}
      el('form').style.display = 'none'
      showOpen(true)
      var l = line(p)
      var made = p.created ? new Date(p.created * 1000).toLocaleDateString() : ''
      el('head').innerHTML = '<div class="sh-proj-head">' +
        '<div style="border-radius:10px;overflow:hidden;border:1px solid var(--line)">' + thumb(p) + '</div>' +
        '<div><h3 style="margin:0 0 4px">' + esc(p.title || 'Untitled') + '</h3>' +
          (l ? '<div style="font-size:13px">' + l + '</div>' : '') +
          (p.description ? '<p style="font-size:13px;margin:8px 0 0;white-space:pre-line">' + esc(p.description) + '</p>' : '') +
          '<div style="font-size:12px;opacity:.6;margin-top:8px">Created ' + esc(made) +
            (p.username ? ' by ' + esc(p.username) : '') + ' &middot; ' + esc(count(p, noun).toLowerCase()) + '</div>' +
          '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">' +
            '<button type="button" class="btn-sm" data-proj-edit>Edit details</button>' +
            (p.can_delete ? '<button type="button" class="btn-sm" style="color:var(--tally);border-color:var(--tally)" data-proj-del>Delete project</button>' : '') +
          '</div></div></div>'
      var head = el('head')
      head.querySelector('[data-proj-edit]').addEventListener('click', function(){ form(p) })
      var del = head.querySelector('[data-proj-del]')
      if(del) del.addEventListener('click', function(){ remove(p) })
      var fresh = !!S.fresh
      S.fresh = false
      if(o.onOpen) o.onOpen(p, fresh, !!refresh)
    }

    function closeProject(){
      S.open = null
      window.PRISM_PROJECT[kind] = null
      try{ localStorage.removeItem(KEY) }catch(e){}
      showOpen(false)
      if(o.onClose) o.onClose()
    }

    async function remove(p){
      if(p.count > 0){
        alert('"' + p.name + '" still holds ' + p.count + ' ' + (p.count === 1 ? noun[0] : noun[1]) +
          '. Delete them, or move them to another project, before deleting the project.')
        return
      }
      if(!confirm('Delete the project "' + p.name + '"? It holds no ' + noun[1] + '. Its details and thumbnail will be removed. This cannot be undone.')) return
      try{
        var r = await (await fetch('/api/' + kind + '/projects/' + p.project_id, {method: 'DELETE'})).json()
        if(!r.ok){ alert(r.error || 'Could not delete the project.'); return }
        closeProject()
        load()
      }catch(e){ alert('Could not delete the project: ' + e) }
    }

    el('new').addEventListener('click', function(){ form() })
    el('cancel').addEventListener('click', function(){ form(null, true) })
    el('save').addEventListener('click', save)
    el('hasdue').addEventListener('change', dueToggle)
    el('filter').addEventListener('change', filterChanged)
    el('back').addEventListener('click', function(){ closeProject(); load() })

    if(work) work.style.display = 'none'

    var ctl = {
      reload: load,
      moveSelect: moveSelect,
      projects: function(){ return S.projects || [] },
      currentId: function(){ return S.open },
      current: function(){ return S.open ? find(S.open) : null },
      loaded: function(){ return S.projects !== null }
    }
    return ctl
  }

  window.PrismProjects = {create: create}
})()
