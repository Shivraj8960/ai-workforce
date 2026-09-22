const API_BASE = "http://127.0.0.1:8000";

async function api(path) {
    const response = await fetch(`${API_BASE}${path}`);

    if (!response.ok) {
        throw new Error(`API ${response.status}: ${response.statusText}`);
    }

    return response.json();
}

function setText(id, value) {
    const element = document.getElementById(id);
    if (element) {
        element.textContent = value;
    }
}

async function loadDashboard() {
    try {
        const health = await api("/health");

        setText("employeeCount", health.employees ?? 0);
        setText("taskCount", health.tasks ?? 0);
        setText(
            "backendStatus",
            health.status === "healthy"
                ? "Nex AI Online"
                : "Backend Offline"
        );

        const dot = document.getElementById("statusDot");

        if (dot) {
            if (health.status === "healthy") {
                dot.classList.add("online");
            } else {
                dot.classList.remove("online");
            }
        }

        await loadTasks();
    } catch (error) {
        console.error("Dashboard API error:", error);

        setText("backendStatus", "Backend Offline");

        const dot = document.getElementById("statusDot");
        if (dot) {
            dot.classList.remove("online");
        }
    }
}

async function loadTasks() {
    try {
        const data = await api("/tasks");
        const tasks = data.tasks || [];

        const completed = tasks.filter(
            task => task.status === "completed"
        ).length;

        const failed = tasks.filter(
            task => task.status === "failed"
        ).length;

        setText("completedCount", completed);
        setText("failedCount", failed);

        const container = document.getElementById("taskList");

        if (!container) {
            console.warn("taskList element not found");
            return;
        }

        container.innerHTML = "";

        tasks.slice(-5).reverse().forEach(task => {
            const item = document.createElement("div");
            item.className = "task";

            item.innerHTML = `
                <div class="task-top">
                    <span class="task-name">${escapeHtml(task.title)}</span>
                    <span class="badge">${escapeHtml(task.status)}</span>
                </div>
                <div class="task-meta">
                    ${escapeHtml(task.id)} ·
                    ${escapeHtml(task.assigned_to || "Unassigned")}
                </div>
            `;

            container.appendChild(item);
        });

        if (tasks.length === 0) {
            container.innerHTML =
                '<div class="task-meta">No tasks yet.</div>';
        }
    } catch (error) {
        console.error("Task API error:", error);
    }
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

async function createWorkforceGoal() {
    const goal = prompt("Enter your workforce goal:");

    if (!goal || !goal.trim()) {
        return;
    }

    const button = document.getElementById("createGoalBtn");

    try {
        if (button) {
            button.disabled = true;
            button.textContent = "Creating...";
        }

        const response = await fetch(`${API_BASE}/plan`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                goal: goal.trim()
            })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || "Planning failed");
        }

        console.log("Workforce created:", data);

        setText(
            "backendStatus",
            `Workforce created — ${data.plan?.tasks?.length ?? 0} tasks`
        );

        await loadDashboard();

    } catch (error) {
        console.error("Planning error:", error);

        setText(
            "backendStatus",
            `Planning failed: ${error.message}`
        );

    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "Create Workforce Goal";
        }
    }
}

document.addEventListener("DOMContentLoaded", () => {
    loadDashboard();

    const button = document.getElementById("createGoalBtn");

    if (button) {
        button.addEventListener("click", createWorkforceGoal);
    } else {
        console.error("createGoalBtn not found");
    }
});

/* Nex AI Workforce Control */
(function setupWorkforceNavigation() {
    let dashboardHTML = "";

    function renderWorkforce() {
        const main = document.querySelector(".main");
        if (!main) return;

        main.innerHTML = `
            <header class="topbar">
                <div>
                    <h1>Workforce</h1>
                    <div class="subtitle">Create, inspect, and execute autonomous AI workflows.</div>
                </div>
                <div class="status">
                    <span class="dot online"></span>
                    Nex AI Online
                </div>
            </header>

            <section class="card" style="margin-bottom:18px;">
                <div class="card-header">
                    <h2>Create Workforce Plan</h2>
                </div>

                <p style="color:#7d8899;font-size:13px;margin-bottom:14px;">
                    Give Nex AI a goal. The planner will create a multi-agent task workflow.
                </p>

                <textarea
                    id="workforceGoal"
                    placeholder="Example: Research the AI market, analyze the findings, review the analysis, and prepare a final report."
                    style="width:100%;min-height:110px;padding:14px;border-radius:10px;border:1px solid #263143;background:#111822;color:#f4f7fb;resize:vertical;font-family:inherit;"
                ></textarea>

                <div style="display:flex;gap:10px;margin-top:14px;">
                    <button id="planWorkforceBtn">Create Plan</button>
                    <button id="runWorkforceBtn" style="background:#192236;">Run Workforce</button>
                </div>

                <div id="workforceMessage" style="margin-top:14px;color:#8f9bad;font-size:13px;"></div>
            </section>

            <section class="card">
                <div class="card-header">
                    <h2>Workflow Tasks</h2>
                    <span id="workforceTaskCount" class="view">0 tasks</span>
                </div>
                <div id="workforceTasks">
                    <div class="task-meta">Create a plan to generate tasks.</div>
                </div>
            </section>
        `;

        document
            .getElementById("planWorkforceBtn")
            ?.addEventListener("click", createWorkforcePlan);

        document
            .getElementById("runWorkforceBtn")
            ?.addEventListener("click", runWorkforce);

        loadWorkforceTasks();
    }

    async function createWorkforcePlan() {
        const input = document.getElementById("workforceGoal");
        const message = document.getElementById("workforceMessage");
        const button = document.getElementById("planWorkforceBtn");

        const goal = input?.value.trim();

        if (!goal) {
            message.textContent = "Please enter a workforce goal.";
            return;
        }

        button.disabled = true;
        button.textContent = "Planning...";
        message.textContent = "Nex AI is generating the multi-agent workflow...";

        try {
            const response = await fetch(`${API_BASE}/plan`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({ goal })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.detail || "Planning failed");
            }

            message.textContent =
                `Plan created successfully — ${data.plan?.tasks?.length ?? 0} tasks generated.`;

            await loadWorkforceTasks();
        } catch (error) {
            console.error("Workforce planning error:", error);
            message.textContent = `Planning failed: ${error.message}`;
        } finally {
            button.disabled = false;
            button.textContent = "Create Plan";
        }
    }

    async function runWorkforce() {
        const message = document.getElementById("workforceMessage");
        const button = document.getElementById("runWorkforceBtn");

        button.disabled = true;
        button.textContent = "Running...";
        message.textContent = "Nex AI is executing the available workflow tasks...";

        try {
            const response = await fetch(`${API_BASE}/workforce/run`, {
                method: "POST"
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.detail || "Workforce execution failed");
            }

            message.textContent =
                `Execution finished — ${data.executed?.length ?? 0} completed, ` +
                `${data.failed?.length ?? 0} failed, ` +
                `${data.blocked?.length ?? 0} blocked.`;

            await loadWorkforceTasks();
        } catch (error) {
            console.error("Workforce execution error:", error);
            message.textContent = `Execution failed: ${error.message}`;
        } finally {
            button.disabled = false;
            button.textContent = "Run Workforce";
        }
    }

    async function loadWorkforceTasks() {
        const container = document.getElementById("workforceTasks");
        const count = document.getElementById("workforceTaskCount");

        if (!container) return;

        try {
            const data = await api("/tasks");
            const tasks = data.tasks || [];

            count.textContent = `${tasks.length} tasks`;
            container.innerHTML = "";

            tasks.slice().reverse().slice(0, 12).forEach(task => {
                const item = document.createElement("div");
                item.className = "task";

                item.innerHTML = `
                    <div class="task-top">
                        <span class="task-name">${escapeHtml(task.title)}</span>
                        <span class="badge">${escapeHtml(task.status)}</span>
                    </div>
                    <div class="task-meta">
                        ${escapeHtml(task.id)} ·
                        ${escapeHtml(task.assigned_to || "Unassigned")}
                    </div>
                `;

                container.appendChild(item);
            });

            if (tasks.length === 0) {
                container.innerHTML =
                    '<div class="task-meta">No workflow tasks yet.</div>';
            }
        } catch (error) {
            console.error("Workforce task loading error:", error);
            container.innerHTML =
                `<div class="task-meta">Unable to load workflow tasks: ${escapeHtml(error.message)}</div>`;
        }
    }

    function setupNavigation() {
        const main = document.querySelector(".main");
        if (!main) return;

        dashboardHTML = main.innerHTML;

        document.querySelectorAll(".nav-item").forEach(item => {
            item.addEventListener("click", () => {
                document.querySelectorAll(".nav-item")
                    .forEach(nav => nav.classList.remove("active"));

                item.classList.add("active");

                if (item.textContent.trim() === "Workforce") {
                    renderWorkforce();
                } else if (item.textContent.trim() === "Tasks") {
                    renderTasks();
                } else if (item.textContent.trim() === "Agents") {
                    renderAgents();
                } else if (item.textContent.trim() === "Memory") {
                    renderMemory();
                } else if (item.textContent.trim() === "Execution") {
                    renderExecution();
                } else if (item.textContent.trim() === "Dashboard") {
                    main.innerHTML = dashboardHTML;
                    loadDashboard();
                } else {
                    main.innerHTML = `
                        <header class="topbar">
                            <div>
                                <h1>${escapeHtml(item.textContent.trim())}</h1>
                                <div class="subtitle">
                                    Nex AI ${escapeHtml(item.textContent.trim())} workspace.
                                </div>
                            </div>
                            <div class="status">
                                <span class="dot online"></span>
                                Nex AI Online
                            </div>
                        </header>

                        <section class="card">
                            <h2>${escapeHtml(item.textContent.trim())}</h2>
                            <p style="color:#7d8899;margin-top:10px;">
                                This Nex AI module is ready for integration.
                            </p>
                        </section>
                    `;
                }
            });
        });
    }

    document.addEventListener("DOMContentLoaded", setupNavigation);
})();

async function renderMemory() {
    const main = document.querySelector(".main");
    if (!main) return;

    const agents = [
        "researcher",
        "analyst",
        "writer",
        "designer",
        "reviewer",
        "executor"
    ];

    main.innerHTML = `
        <header class="topbar">
            <div>
                <h1>Agent Memory</h1>
                <div class="subtitle">Persistent knowledge created by the Nex AI workforce.</div>
            </div>
            <div class="status">
                <span class="dot online"></span>
                Nex AI Online
            </div>
        </header>

        <section class="card">
            <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;">
                <select id="memoryAgentFilter"
                        style="padding:12px;border-radius:8px;border:1px solid #263044;background:#111722;color:#fff;">
                    <option value="all">All Agents</option>
                    ${agents.map(agent => `
                        <option value="${agent}">${agent.charAt(0).toUpperCase() + agent.slice(1)}</option>
                    `).join("")}
                </select>
            </div>

            <div id="memoryStats" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px;"></div>
            <div id="memoryWorkspace">Loading agent memory...</div>
        </section>
    `;

    let allMemory = [];

    async function loadMemory() {
        const workspace = document.getElementById("memoryWorkspace");

        try {
            const responses = await Promise.all(
                agents.map(async agent => {
                    const data = await api(`/agents/${encodeURIComponent(agent)}/memory`);
                    const memories = data.memory || data.memories || [];
                    return memories.map(memory => ({
                        ...memory,
                        agent: memory.agent || agent
                    }));
                })
            );

            allMemory = responses.flat();
            renderMemoryList();
        } catch (error) {
            workspace.innerHTML =
                `<div class="task-meta">Unable to load memory: ${escapeHtml(error.message)}</div>`;
        }
    }

    function renderMemoryList() {
        const workspace = document.getElementById("memoryWorkspace");
        const filter = document.getElementById("memoryAgentFilter").value;

        const filtered = filter === "all"
            ? allMemory
            : allMemory.filter(memory => memory.agent === filter);

        document.getElementById("memoryStats").innerHTML = `
            <div class="badge">Total Memories: ${allMemory.length}</div>
            <div class="badge">Agents: ${agents.length}</div>
            <div class="badge">Showing: ${filtered.length}</div>
        `;

        if (!filtered.length) {
            workspace.innerHTML =
                `<div class="task-meta">No memory recorded for this selection yet.</div>`;
            return;
        }

        workspace.innerHTML = filtered.map(memory => `
            <div class="task" style="margin-bottom:12px;">
                <div class="task-top">
                    <span class="task-name">
                        ${escapeHtml(
                            (memory.agent || "agent").charAt(0).toUpperCase() +
                            (memory.agent || "agent").slice(1)
                        )}
                    </span>

                    <span class="badge">
                        ${escapeHtml(memory.task_id || "No task")}
                    </span>
                </div>

                <div class="task-meta" style="margin-top:10px;white-space:pre-wrap;line-height:1.5;">
                    ${escapeHtml(memory.memory || memory.content || String(memory))}
                </div>

                ${memory.created_at ? `
                    <div class="task-meta" style="margin-top:10px;">
                        Created: ${escapeHtml(memory.created_at)}
                    </div>
                ` : ""}
            </div>
        `).join("");
    }

    document
        .getElementById("memoryAgentFilter")
        .addEventListener("change", renderMemoryList);

    await loadMemory();
}


async function renderAgents() {
    const main = document.querySelector(".main");
    if (!main) return;

    const agents = [
        {
            key: "researcher",
            name: "Researcher",
            description: "Investigates tasks, gathers evidence, identifies assumptions and information gaps."
        },
        {
            key: "analyst",
            name: "Analyst",
            description: "Analyzes information, identifies patterns, risks, trade-offs and practical conclusions."
        },
        {
            key: "writer",
            name: "Writer",
            description: "Transforms information into clear, structured and professional deliverables."
        },
        {
            key: "designer",
            name: "Designer",
            description: "Creates layouts, UX concepts, design specifications and implementation guidance."
        },
        {
            key: "reviewer",
            name: "Reviewer",
            description: "Reviews work for accuracy, completeness, consistency, risks and missing requirements."
        },
        {
            key: "executor",
            name: "Executor",
            description: "Turns approved work into actionable execution steps and final deliverables."
        }
    ];

    main.innerHTML = `
        <header class="topbar">
            <div>
                <h1>AI Agents</h1>
                <div class="subtitle">Your autonomous Nex AI workforce.</div>
            </div>
            <div class="status">
                <span class="dot online"></span>
                Nex AI Online
            </div>
        </header>

        <section class="card">
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;">
                ${agents.map(agent => `
                    <div class="task agent-card"
                         data-agent="${agent.key}"
                         style="cursor:pointer;min-height:150px;">
                        <div style="display:flex;align-items:center;gap:12px;">
                            <div style="width:42px;height:42px;border-radius:10px;background:#18233a;display:flex;align-items:center;justify-content:center;color:#8da2ff;font-weight:700;font-size:18px;">
                                ${agent.name.charAt(0)}
                            </div>
                            <div>
                                <div class="task-name">${agent.name}</div>
                                <div class="task-meta">${agent.key}</div>
                            </div>
                        </div>

                        <p style="color:#8d98aa;line-height:1.5;margin-top:16px;">
                            ${agent.description}
                        </p>

                        <div class="badge">View Memory →</div>
                    </div>
                `).join("")}
            </div>
        </section>
    `;

    main.querySelectorAll("[data-agent]").forEach(card => {
        card.addEventListener("click", () => {
            showAgentMemory(card.dataset.agent, agents);
        });
    });
}

async function showAgentMemory(agentKey, agents) {
    const main = document.querySelector(".main");
    const agent = agents.find(item => item.key === agentKey);

    main.innerHTML = `
        <header class="topbar">
            <div>
                <h1>${escapeHtml(agent?.name || agentKey)}</h1>
                <div class="subtitle">${escapeHtml(agent?.description || "Nex AI agent memory")}</div>
            </div>
            <div class="status">
                <span class="dot online"></span>
                Nex AI Online
            </div>
        </header>

        <section class="card">
            <button id="backToAgents"
                    style="padding:9px 14px;border:0;border-radius:7px;background:#1b2535;color:#fff;cursor:pointer;margin-bottom:18px;">
                ← Back to Agents
            </button>

            <h2>Agent Memory</h2>
            <div id="agentMemory" style="margin-top:18px;color:#aab4c4;">
                Loading memory...
            </div>
        </section>
    `;

    document.getElementById("backToAgents").addEventListener("click", () => {
        renderAgents();
    });

    try {
        const data = await api(`/agents/${encodeURIComponent(agentKey)}/memory`);
        const memories = data.memory || data.memories || [];

        const container = document.getElementById("agentMemory");

        if (!memories.length) {
            container.innerHTML = "No memory recorded for this agent yet.";
            return;
        }

        container.innerHTML = memories.map(memory => `
            <div class="task" style="margin-bottom:10px;">
                <div class="task-top">
                    <span class="task-name">
                        ${escapeHtml(memory.task_id || "Agent Memory")}
                    </span>
                </div>
                <div class="task-meta" style="white-space:pre-wrap;margin-top:10px;">
                    ${escapeHtml(memory.memory || memory.content || String(memory))}
                </div>
            </div>
        `).join("");
    } catch (error) {
        document.getElementById("agentMemory").innerHTML =
            `<div class="task-meta">Unable to load agent memory: ${escapeHtml(error.message)}</div>`;
    }
}


async function renderTasks() {
    const main = document.querySelector(".main");
    if (!main) return;

    main.innerHTML = `
        <header class="topbar">
            <div>
                <h1>Tasks</h1>
                <div class="subtitle">Monitor, inspect, and manage Nex AI tasks.</div>
            </div>
            <div class="status">
                <span class="dot online"></span>
                Nex AI Online
            </div>
        </header>

        <section class="card">
            <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px;">
                <input id="taskSearch"
                       placeholder="Search tasks..."
                       style="flex:1;min-width:220px;padding:12px;border-radius:8px;border:1px solid #263044;background:#111722;color:#fff;">
                <select id="taskStatusFilter"
                        style="padding:12px;border-radius:8px;border:1px solid #263044;background:#111722;color:#fff;">
                    <option value="all">All Status</option>
                    <option value="pending">Pending</option>
                    <option value="running">Running</option>
                    <option value="completed">Completed</option>
                    <option value="failed">Failed</option>
                    <option value="blocked">Blocked</option>
                </select>
            </div>

            <div id="taskStats" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px;"></div>
            <div id="tasksWorkspace">Loading tasks...</div>
        </section>
    `;

    let allTasks = [];

    async function refreshTasks() {
        const container = document.getElementById("tasksWorkspace");

        try {
            const data = await api("/tasks");
            allTasks = data.tasks || [];
            renderTaskList();
        } catch (error) {
            container.innerHTML =
                `<div class="task-meta">Unable to load tasks: ${escapeHtml(error.message)}</div>`;
        }
    }

    function renderTaskList() {
        const container = document.getElementById("tasksWorkspace");
        const search = (document.getElementById("taskSearch")?.value || "").toLowerCase();
        const status = document.getElementById("taskStatusFilter")?.value || "all";

        const filtered = allTasks.filter(task => {
            const matchesSearch =
                !search ||
                String(task.id || "").toLowerCase().includes(search) ||
                String(task.title || "").toLowerCase().includes(search) ||
                String(task.assigned_to || "").toLowerCase().includes(search);

            const matchesStatus =
                status === "all" || task.status === status;

            return matchesSearch && matchesStatus;
        });

        const completed = allTasks.filter(t => t.status === "completed").length;
        const pending = allTasks.filter(t => t.status === "pending").length;
        const failed = allTasks.filter(t => t.status === "failed").length;

        document.getElementById("taskStats").innerHTML = `
            <div class="badge">Total: ${allTasks.length}</div>
            <div class="badge">Completed: ${completed}</div>
            <div class="badge">Pending: ${pending}</div>
            <div class="badge">Failed: ${failed}</div>
        `;

        if (!filtered.length) {
            container.innerHTML =
                `<div class="task-meta">No tasks match your filters.</div>`;
            return;
        }

        container.innerHTML = filtered.map(task => {
            const dependencies = Array.isArray(task.dependencies)
                ? task.dependencies.join(", ") || "None"
                : String(task.dependencies || "None");

            return `
                <div class="task"
                     data-task-id="${escapeHtml(task.id)}"
                     style="cursor:pointer;margin-bottom:10px;">
                    <div class="task-top">
                        <span class="task-name">${escapeHtml(task.title || "Untitled task")}</span>
                        <span class="badge">${escapeHtml(task.status || "unknown")}</span>
                    </div>

                    <div class="task-meta">
                        ${escapeHtml(task.id || "")}
                        · ${escapeHtml(task.assigned_to || "Unassigned")}
                        · Priority: ${escapeHtml(task.priority || "medium")}
                        · Dependencies: ${escapeHtml(dependencies)}
                    </div>
                </div>
            `;
        }).join("");

        container.querySelectorAll("[data-task-id]").forEach(element => {
            element.addEventListener("click", () => {
                const task = allTasks.find(t => t.id === element.dataset.taskId);
                if (task) showTaskDetails(task);
            });
        });
    }

    async function showTaskDetails(task) {
        const container = document.getElementById("tasksWorkspace");

        container.innerHTML = `
            <div style="margin-bottom:18px;">
                <button id="backToTasks"
                        style="padding:9px 14px;border:0;border-radius:7px;background:#1b2535;color:#fff;cursor:pointer;">
                    ← Back to Tasks
                </button>
            </div>

            <div class="card">
                <h2>${escapeHtml(task.title || "Untitled task")}</h2>

                <div class="task-meta" style="margin-top:10px;">
                    ID: ${escapeHtml(task.id || "")}
                </div>

                <div class="task-meta">
                    Agent: ${escapeHtml(task.assigned_to || "Unassigned")}
                    · Status: ${escapeHtml(task.status || "unknown")}
                    · Priority: ${escapeHtml(task.priority || "medium")}
                </div>

                <div style="margin-top:20px;">
                    <h3>Description</h3>
                    <p style="color:#aab4c4;white-space:pre-wrap;">
                        ${escapeHtml(task.description || "No description available.")}
                    </p>
                </div>

                <div style="margin-top:20px;">
                    <h3>Dependencies</h3>
                    <p style="color:#aab4c4;">
                        ${escapeHtml(
                            Array.isArray(task.dependencies)
                                ? task.dependencies.join(", ") || "None"
                                : String(task.dependencies || "None")
                        )}
                    </p>
                </div>

                <div style="margin-top:20px;">
                    <h3>Result</h3>
                    <pre id="taskResult"
                         style="white-space:pre-wrap;color:#aab4c4;background:#0b1018;padding:14px;border-radius:8px;overflow:auto;">Loading result...</pre>
                </div>

                <div style="margin-top:20px;">
                    <h3>Execution History</h3>
                    <div id="taskHistory" style="color:#aab4c4;">Loading history...</div>
                </div>
            </div>
        `;

        document.getElementById("backToTasks").addEventListener("click", refreshTasks);

        const resultElement = document.getElementById("taskResult");

        try {
            let result = task.result || "No result available.";
            try {
                const parsed = JSON.parse(result);
                result = parsed.result || result;
            } catch (_) {}
            resultElement.textContent = result;
        } catch (_) {
            resultElement.textContent = "No result available.";
        }

        try {
            const historyData = await api(`/tasks/${encodeURIComponent(task.id)}/history`);
            const history = historyData.history || [];

            document.getElementById("taskHistory").innerHTML =
                history.length
                    ? history.map(item => `
                        <div class="task" style="margin-bottom:8px;">
                            <div class="task-top">
                                <span class="task-name">
                                    Attempt ${escapeHtml(item.attempt ?? "")}
                                </span>
                                <span class="badge">
                                    ${escapeHtml(item.status || "unknown")}
                                </span>
                            </div>
                            <div class="task-meta">
                                Agent: ${escapeHtml(item.agent || "")}
                                · Provider: ${escapeHtml(item.provider || "unknown")}
                            </div>
                        </div>
                    `).join("")
                    : "No execution history available.";
        } catch (error) {
            document.getElementById("taskHistory").textContent =
                `History unavailable: ${error.message}`;
        }
    }

    document.getElementById("taskSearch").addEventListener("input", renderTaskList);
    document.getElementById("taskStatusFilter").addEventListener("change", renderTaskList);

    await refreshTasks();
}



async function renderExecution() {
    const main = document.querySelector(".main");
    if (!main) return;

    main.innerHTML = `
        <header class="topbar">
            <div>
                <h1>Execution</h1>
                <div class="subtitle">Inspect real Nex AI execution history, providers, attempts, and recovery.</div>
            </div>
            <div class="status">
                <span class="dot online"></span>
                Nex AI Online
            </div>
        </header>

        <section class="card">
            <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;">
                <select id="executionTaskSelect"
                    style="flex:1;min-width:260px;padding:12px;border-radius:8px;border:1px solid #263044;background:#111722;color:#fff;">
                    <option value="">Loading tasks...</option>
                </select>
                <span id="executionStatus"
                    style="padding:7px 10px;border-radius:999px;background:#102d20;color:#4ade80;font-size:12px;">
                    Ready
                </span>
            </div>

            <div id="executionHistory"
                style="margin-top:18px;">
                <div class="task-meta">Select a task to inspect execution history.</div>
            </div>
        </section>
    `;

    const select = document.getElementById("executionTaskSelect");
    const historyContainer = document.getElementById("executionHistory");
    const status = document.getElementById("executionStatus");

    try {
        const data = await api("/tasks");
        const tasks = data.tasks || [];

        select.innerHTML = "";

        if (!tasks.length) {
            select.innerHTML = '<option value="">No tasks available</option>';
            historyContainer.innerHTML =
                '<div class="task-meta">No tasks available.</div>';
            return;
        }

        tasks.slice().reverse().forEach(task => {
            const option = document.createElement("option");
            option.value = task.id;
            option.textContent =
                `${task.id} — ${task.title} [${task.status}]`;
            select.appendChild(option);
        });

        const loadHistory = async (taskId) => {
            if (!taskId) return;

            historyContainer.innerHTML =
                '<div class="task-meta">Loading execution history...</div>';
            status.textContent = "Loading";

            try {
                const data = await api(`/tasks/${encodeURIComponent(taskId)}/history`);
                const history = data.history || [];

                if (!history.length) {
                    historyContainer.innerHTML =
                        '<div class="task-meta">No execution history recorded for this task.</div>';
                    status.textContent = "No history";
                    return;
                }

                historyContainer.innerHTML = `
                    <div style="margin-bottom:14px;">
                        <strong>${escapeHtml(taskId)}</strong>
                        <span class="task-meta">
                            · ${history.length} execution${history.length === 1 ? "" : "s"}
                        </span>
                    </div>
                    ${history.map((entry, index) => `
                        <div style="padding:16px 0;border-bottom:1px solid #202838;">
                            <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;">
                                <strong>Execution #${history.length - index} · Attempt ${escapeHtml(String(entry.attempt ?? 1))}</strong>
                                <span class="badge">${escapeHtml(entry.status || "unknown")}</span>
                            </div>

                            <div class="task-meta" style="margin-top:8px;">
                                Agent: ${escapeHtml(entry.agent || "Unknown")}
                                · Provider: ${escapeHtml(entry.provider || "Unknown")}
                                · Recovery: ${escapeHtml(entry.recovery || "none")}
                            </div>

                            <div class="task-meta" style="margin-top:5px;">
                                ${escapeHtml(entry.created_at || "")}
                            </div>

                            ${entry.failure_analysis ? `
                                <details style="margin-top:12px;">
                                    <summary style="cursor:pointer;color:#8da2ff;">
                                        Failure analysis
                                    </summary>
                                    <pre style="white-space:pre-wrap;overflow:auto;margin-top:10px;color:#9aa6b5;">${escapeHtml(entry.failure_analysis)}</pre>
                                </details>
                            ` : ""}

                            ${entry.result ? `
                                <details style="margin-top:12px;">
                                    <summary style="cursor:pointer;color:#8da2ff;">
                                        Execution result
                                    </summary>
                                    <pre style="white-space:pre-wrap;overflow:auto;margin-top:10px;color:#9aa6b5;max-height:420px;">${escapeHtml(entry.result)}</pre>
                                </details>
                            ` : ""}
                        </div>
                    `).join("")}
                `;

                status.textContent = `${history.length} execution${history.length === 1 ? "" : "s"}`;
            } catch (error) {
                console.error("Execution history error:", error);
                historyContainer.innerHTML =
                    `<div class="task-meta">Unable to load execution history: ${escapeHtml(error.message)}</div>`;
                status.textContent = "Error";
            }
        };

        select.addEventListener("change", () => {
            loadHistory(select.value);
        });

        await loadHistory(select.value);
    } catch (error) {
        console.error("Execution task loading error:", error);
        select.innerHTML = '<option value="">Unable to load tasks</option>';
        historyContainer.innerHTML =
            `<div class="task-meta">Unable to load tasks: ${escapeHtml(error.message)}</div>`;
        status.textContent = "Error";
    }
}
