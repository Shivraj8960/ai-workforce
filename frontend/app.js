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

function formatExecutionResult(value) {
    const text = String(value || "");

    if (!text.trim()) {
        return '<div class="task-meta">No execution result recorded.</div>';
    }

    return text
        .split("\n")
        .map(line => {
            const escaped = escapeHtml(line.trim());

            if (!escaped) {
                return '<div style="height:8px;"></div>';
            }

            if (/^\*\*.+\*\*$/.test(escaped)) {
                return `<h3 style="margin:14px 0 8px;">${escaped.replace(/\*\*/g, "")}</h3>`;
            }

            if (/^[-*]\s+/.test(escaped)) {
                const content = escaped.replace(/^[-*]\s+/, "");
                return `<div style="margin:5px 0 5px 16px;">• ${content.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")}</div>`;
            }

            if (/^\d+\.\s+/.test(escaped)) {
                const content = escaped.replace(/^\d+\.\s+/, "");
                return `<div style="margin:5px 0 5px 16px;">${escaped.match(/^\d+/)[0]}. ${content.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")}</div>`;
            }

            if (escaped.includes("|") && escaped.split("|").length >= 3) {
                const cells = escaped.split("|").slice(1, -1).map(cell => cell.trim());

                if (cells.length && cells.every(cell => /^[-:]+$/.test(cell))) {
                    return "";
                }

                const formattedCells = cells
                    .map(cell => cell.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"))
                    .map(cell => `<div style="flex:1;padding:7px 10px;border-bottom:1px solid rgba(255,255,255,0.08);">${cell}</div>`)
                    .join("");

                return `<div style="display:flex;gap:0;margin:2px 0;">${formattedCells}</div>`;
            }

            const formatted = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
            return `<div style="margin:6px 0;white-space:pre-wrap;">${formatted}</div>`;
        })
        .join("");
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

        <section class="stats" id="agentOverviewStats">
            <div class="card">
                <div class="stat-label">AI Employees</div>
                <div class="stat-value" id="agentTotalCount">—</div>
            </div>
            <div class="card">
                <div class="stat-label">Assigned Tasks</div>
                <div class="stat-value" id="agentTaskCount">—</div>
            </div>
            <div class="card">
                <div class="stat-label">Completed</div>
                <div class="stat-value" id="agentCompletedCount">—</div>
            </div>
            <div class="card">
                <div class="stat-label">Failed</div>
                <div class="stat-value" id="agentFailedCount">—</div>
            </div>
        </section>

        <section class="card">
            <div class="card-header">
                <h2>Workforce Agents</h2>
                <span class="view">Live backend data</span>
            </div>

            <div id="agentCards"
                 style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;">
                ${agents.map(agent => `
                    <div class="task agent-card"
                         data-agent="${agent.key}"
                         style="cursor:pointer;min-height:190px;padding:18px;border:1px solid #1d2633;border-radius:12px;background:#101722;">
                        <div style="display:flex;align-items:center;gap:12px;">
                            <div style="width:42px;height:42px;border-radius:10px;background:#18233a;display:flex;align-items:center;justify-content:center;color:#8da2ff;font-weight:700;font-size:18px;">
                                ${agent.name.charAt(0)}
                            </div>
                            <div>
                                <div class="task-name">${agent.name}</div>
                                <div class="task-meta">${agent.key}</div>
                            </div>
                        </div>

                        <p style="color:#8d98aa;line-height:1.5;margin:16px 0 12px;">
                            ${agent.description}
                        </p>

                        <div class="agent-live-stats"
                             data-agent-stats="${agent.key}"
                             style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;">
                            <div style="background:#0b111a;border-radius:8px;padding:8px;">
                                <div class="task-meta">Tasks</div>
                                <div style="font-weight:700;margin-top:3px;">—</div>
                            </div>
                            <div style="background:#0b111a;border-radius:8px;padding:8px;">
                                <div class="task-meta">Done</div>
                                <div style="font-weight:700;margin-top:3px;">—</div>
                            </div>
                            <div style="background:#0b111a;border-radius:8px;padding:8px;">
                                <div class="task-meta">Failed</div>
                                <div style="font-weight:700;margin-top:3px;">—</div>
                            </div>
                        </div>

                        <div class="badge">Open Agent Dashboard →</div>
                    </div>
                `).join("")}
            </div>
        </section>
    `;

    try {
        const data = await api("/tasks");
        const tasks = data.tasks || [];

        const agentStats = agents.map(agent => {
            const assigned = tasks.filter(
                task => String(task.assigned_to || "").toLowerCase() === agent.key
            );

            return {
                ...agent,
                total: assigned.length,
                completed: assigned.filter(task => task.status === "completed").length,
                failed: assigned.filter(task => task.status === "failed").length
            };
        });

        const totalTasks = agentStats.reduce((sum, agent) => sum + agent.total, 0);
        const totalCompleted = agentStats.reduce((sum, agent) => sum + agent.completed, 0);
        const totalFailed = agentStats.reduce((sum, agent) => sum + agent.failed, 0);

        setText("agentTotalCount", agents.length);
        setText("agentTaskCount", totalTasks);
        setText("agentCompletedCount", totalCompleted);
        setText("agentFailedCount", totalFailed);

        agentStats.forEach(agent => {
            const card = document.querySelector(
                `[data-agent-stats="${agent.key}"]`
            );

            if (!card) return;

            const values = card.querySelectorAll(
                "div > div:last-child"
            );

            if (values.length >= 3) {
                values[0].textContent = agent.total;
                values[1].textContent = agent.completed;
                values[2].textContent = agent.failed;
            }
        });
    } catch (error) {
        console.error("Unable to load agent statistics:", error);
    }

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
                <div class="subtitle">${escapeHtml(agent?.description || "Nex AI agent dashboard")}</div>
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

            <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;">
                <div style="width:52px;height:52px;border-radius:12px;background:#18233a;display:flex;align-items:center;justify-content:center;color:#8da2ff;font-weight:700;font-size:22px;">
                    ${escapeHtml((agent?.name || agentKey).charAt(0))}
                </div>
                <div>
                    <div style="font-size:22px;font-weight:700;">${escapeHtml(agent?.name || agentKey)}</div>
                    <div class="task-meta">
    ${escapeHtml(agentKey)} · <span id="agentStatus">Checking...</span>
</div>
                </div>
            </div>

            <div id="agentDashboardStats"
                 style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px;">
                <div class="card"><div class="stat-label">Assigned Tasks</div><div class="stat-value">—</div></div>
                <div class="card"><div class="stat-label">Completed</div><div class="stat-value">—</div></div>
                <div class="card"><div class="stat-label">Failed</div><div class="stat-value">—</div></div>
                <div class="card"><div class="stat-label">Success Rate</div><div class="stat-value">—</div></div>
            </div>

            <section style="margin-bottom:24px;">
                <div class="card-header">
                    <h2>Current Tasks</h2>
                    <span class="view" id="currentTaskCount">Loading...</span>
                </div>
                <div id="agentCurrentTasks" style="margin-top:14px;">
                    Loading tasks...
                </div>
            </section>

            <section style="margin-bottom:24px;">
                <div class="card-header">
                    <h2>Recent Executions</h2>
                    <span class="view" id="executionCount">Loading...</span>
                </div>
                <div id="agentExecutions" style="margin-top:14px;">
                    Loading executions...
                </div>
            </section>

            <section>
                <div class="card-header">
                    <h2>Agent Memory</h2>
                    <span class="view" id="memoryCount">Loading...</span>
                </div>
                <div id="agentMemory" style="margin-top:14px;">
                    Loading memory...
                </div>
            </section>
        </section>
    `;

    document.getElementById("backToAgents").addEventListener("click", () => {
        renderAgents();
    });

    try {
        const [tasksData, historyData, memoryData] = await Promise.all([
            api("/tasks"),
            api(`/agents/${encodeURIComponent(agentKey)}/history`),
            api(`/agents/${encodeURIComponent(agentKey)}/memory`)
        ]);

        const tasks = tasksData.tasks || [];
        const history = historyData.history || [];
        const memories = memoryData.memory || memoryData.memories || [];

        const assignedTasks = tasks.filter(
            task => String(task.assigned_to || "").toLowerCase() === agentKey.toLowerCase()
        );

        const running = assignedTasks.some(
            task => task.status === "running"
        );

        const statusElement = document.getElementById("agentStatus");
        if (statusElement) {
            statusElement.textContent = running ? "Working" : "Idle";
        }

        const completed = assignedTasks.filter(
            task => task.status === "completed"
        ).length;

        const failed = assignedTasks.filter(
            task => task.status === "failed"
        ).length;

        const successRate = assignedTasks.length
            ? Math.round((completed / assignedTasks.length) * 100)
            : 0;

        const stats = document.querySelectorAll("#agentDashboardStats .stat-value");

        if (stats.length >= 4) {
            stats[0].textContent = assignedTasks.length;
            stats[1].textContent = completed;
            stats[2].textContent = failed;
            stats[3].textContent = `${successRate}%`;
        }

        const currentTaskCount = document.getElementById("currentTaskCount");
        const currentTaskContainer = document.getElementById("agentCurrentTasks");

        if (currentTaskCount) {
            currentTaskCount.textContent =
                `${assignedTasks.length} task${assignedTasks.length === 1 ? "" : "s"}`;
        }

        if (currentTaskContainer) {
            if (!assignedTasks.length) {
                currentTaskContainer.innerHTML =
                    '<div class="task-meta">No tasks assigned to this agent.</div>';
            } else {
                currentTaskContainer.innerHTML = assignedTasks.map(task => `
                    <div class="task" data-current-task="${escapeHtml(task.id || "")}" style="margin-bottom:10px;cursor:pointer;">
                        <div class="task-top">
                            <span class="task-name">
                                ${escapeHtml(task.id || "Unknown task")}
                            </span>
                            <span class="badge">
                                ${escapeHtml(task.status || "unknown")}
                            </span>
                        </div>

                        <div class="task-meta" style="margin-top:8px;">
                            ${escapeHtml(task.title || task.description || "No task description")}
                        </div>

                        <div class="task-meta" style="margin-top:6px;">
                            Priority: ${escapeHtml(task.priority || "—")}
                        </div>
                    </div>
                `).join("");
            }
        }

        document.querySelectorAll("[data-current-task]").forEach(element => {
            element.addEventListener("click", async () => {
                const taskId = element.dataset.currentTask;
                if (!taskId) return;

                try {
                    const task = await api(`/tasks/${encodeURIComponent(taskId)}`);

                    main.innerHTML = `
                        <header class="topbar">
                            <div>
                                <h1>Task Details</h1>
                                <div class="subtitle">Nex AI task information</div>
                            </div>
                            <div class="status">
                                <span class="dot online"></span>
                                Nex AI Online
                            </div>
                        </header>

                        <section class="card">
                            <button id="backToAgentDashboard"
                                style="padding:9px 14px;border:0;border-radius:7px;background:#1b2535;color:#fff;cursor:pointer;margin-bottom:18px;">
                                ← Back to ${escapeHtml(agent?.name || agentKey)}
                            </button>

                            <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px;">
                                <div>
                                    <div style="font-size:24px;font-weight:700;">
                                        ${escapeHtml(task.id || "Unknown task")}
                                    </div>
                                    <div class="task-meta" style="margin-top:5px;">
                                        ${escapeHtml(task.title || "Untitled task")}
                                    </div>
                                </div>
                                <span class="badge">
                                    ${escapeHtml(task.status || "unknown")}
                                </span>
                            </div>

                            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:22px;">
                                <div class="card">
                                    <div class="stat-label">Assigned To</div>
                                    <div style="font-weight:600;margin-top:6px;">
                                        ${escapeHtml(task.assigned_to || "Unassigned")}
                                    </div>
                                </div>

                                <div class="card">
                                    <div class="stat-label">Priority</div>
                                    <div style="font-weight:600;margin-top:6px;">
                                        ${escapeHtml(task.priority || "—")}
                                    </div>
                                </div>

                                <div class="card">
                                    <div class="stat-label">Approval</div>
                                    <div style="font-weight:600;margin-top:6px;">
                                        ${escapeHtml(task.approval_status || "—")}
                                    </div>
                                </div>

                                <div class="card">
                                    <div class="stat-label">Provider</div>
                                    <div style="font-weight:600;margin-top:6px;">
                                        ${escapeHtml(task.provider || "—")}
                                    </div>
                                </div>
                            </div>

                            <div style="margin-bottom:22px;">
                                <h2>Description</h2>
                                <div class="task-meta" style="white-space:pre-wrap;margin-top:10px;">
                                    ${escapeHtml(task.description || "No description available.")}
                                </div>
                            </div>

                            <section style="margin-top:24px;">
                                <div class="card-header">
                                    <h2>Execution History</h2>
                                    <span class="view" id="taskExecutionCount">Loading...</span>
                                </div>
                                <div id="taskExecutionHistory" style="margin-top:14px;">
                                    Loading execution history...
                                </div>
                            </section>

                            <div>
                                <h2>Dependencies</h2>
                                <div class="task-meta" style="margin-top:10px;">
                                    ${task.dependencies?.length
                                        ? task.dependencies.map(dep => escapeHtml(dep)).join(", ")
                                        : "No dependencies"}
                                </div>
                            </div>
                        </section>
                    `;

                    const taskHistory = await api(
                        `/tasks/${encodeURIComponent(taskId)}/history`
                    );

                    const taskHistoryItems = taskHistory.history || [];
                    const taskExecutionCount = document.getElementById("taskExecutionCount");
                    const taskExecutionHistory = document.getElementById("taskExecutionHistory");

                    if (taskExecutionCount) {
                        taskExecutionCount.textContent =
                            `${taskHistoryItems.length} execution${taskHistoryItems.length === 1 ? "" : "s"}`;
                    }

                    if (taskExecutionHistory) {
                        if (!taskHistoryItems.length) {
                            taskExecutionHistory.innerHTML =
                                '<div class="task-meta">No execution history recorded.</div>';
                        } else {
                            taskExecutionHistory.innerHTML = taskHistoryItems.map(execution => `
                                <div class="task" style="margin-bottom:10px;">
                                    <div class="task-top">
                                        <span class="task-name">
                                            Attempt ${escapeHtml(String(execution.attempt ?? "—"))}
                                        </span>
                                        <span class="badge">
                                            ${escapeHtml(execution.status || "unknown")}
                                        </span>
                                    </div>

                                    <div class="task-meta" style="margin-top:8px;">
                                        Agent: ${escapeHtml(execution.agent || "—")}
                                        · Provider: ${escapeHtml(execution.provider || "—")}
                                    </div>

                                    <div class="task-meta" style="margin-top:6px;">
                                        ${escapeHtml(execution.created_at || "—")}
                                    </div>

                                    ${
                                        execution.recovery
                                            ? `<div class="task-meta" style="margin-top:6px;">
                                                Recovery: ${escapeHtml(execution.recovery)}
                                              </div>`
                                            : ""
                                    }
                                </div>
                            `).join("");
                        }
                    }

                    document.getElementById("backToAgentDashboard").addEventListener("click", () => {
        showAgentMemory(agentKey, agents);
    });

        } catch (error) {
            alert(`Unable to load task: ${error.message}`);
        }
    });
});

        document.getElementById("executionCount").textContent =
            `${history.length} execution${history.length === 1 ? "" : "s"}`;

        const executionContainer = document.getElementById("agentExecutions");

        if (!history.length) {
            executionContainer.innerHTML =
                '<div class="task-meta">No executions recorded for this agent yet.</div>';
        } else {
            executionContainer.innerHTML = history.slice(0, 10).map(execution => `
                <div class="task" data-execution-task="${escapeHtml(execution.task_id || "")}" data-execution-id="${escapeHtml(String(execution.id ?? ""))}" style="margin-bottom:10px;cursor:pointer;">
                    <div class="task-top">
                        <span class="task-name">
                            ${escapeHtml(execution.task_id || "Unknown task")}
                        </span>
                        <span class="badge">
                            ${escapeHtml(execution.status || "unknown")}
                        </span>
                    </div>

                    <div class="task-meta" style="margin-top:8px;">
                        Attempt: ${escapeHtml(String(execution.attempt ?? "—"))}
                        · Provider: ${escapeHtml(execution.provider || "—")}
                        · ${escapeHtml(execution.created_at || "—")}
                    </div>

                    ${
                        execution.recovery
                            ? `<div class="task-meta" style="margin-top:6px;">
                                Recovery: ${escapeHtml(execution.recovery)}
                               </div>`
                            : ""
                    }
                </div>
            `).join("");
        }

        document.querySelectorAll("[data-execution-task]").forEach(element => {
            element.addEventListener("click", async () => {
                const taskId = element.dataset.executionTask;
                if (!taskId) return;
                try {
                    const historyData = await api(
                    `/tasks/${encodeURIComponent(taskId)}/history`
                );
                const executionId = element.dataset.executionId;
                const execution = (historyData.history || []).find(
                    item => String(item.id) === String(executionId)
                );
                if (!execution) {
                    throw new Error("Execution record not found");
                }

                main.innerHTML = `
                    <header class="topbar">
                        <div>
                            <h1>Execution Details</h1>
                            <div class="subtitle">Nex AI execution record</div>
                        </div>
                        <div class="status">
                            <span class="dot online"></span>
                            Nex AI Online
                        </div>
                    </header>

                    <section class="card">
                        <button id="backToAgentDashboard"
                            style="padding:9px 14px;border:0;border-radius:7px;background:#1b2535;color:#fff;cursor:pointer;margin-bottom:18px;">
                            ← Back to ${escapeHtml(agent?.name || agentKey)}
                        </button>

                        <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px;">
                            <div>
                                <div style="font-size:24px;font-weight:700;">
                                    Execution #${escapeHtml(String(execution.id ?? "—"))}
                                </div>
                                <div class="task-meta" style="margin-top:5px;">
                                    Task ${escapeHtml(execution.task_id || "Unknown task")}
                                </div>
                            </div>
                            <span class="badge">
                                ${escapeHtml(execution.status || "unknown")}
                            </span>
                        </div>

                        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:22px;">
                            <div class="card">
                                <div class="stat-label">Agent</div>
                                <div style="font-weight:600;margin-top:6px;">
                                    ${escapeHtml(execution.agent || "—")}
                                </div>
                            </div>

                            <div class="card">
                                <div class="stat-label">Attempt</div>
                                <div style="font-weight:600;margin-top:6px;">
                                    ${escapeHtml(String(execution.attempt ?? "—"))}
                                </div>
                            </div>

                            <div class="card">
                                <div class="stat-label">Provider</div>
                                <div style="font-weight:600;margin-top:6px;">
                                    ${escapeHtml(execution.provider || "—")}
                                </div>
                            </div>

                            <div class="card">
                                <div class="stat-label">Created</div>
                                <div style="font-weight:600;margin-top:6px;">
                                    ${escapeHtml(execution.created_at || "—")}
                                </div>
                            </div>
                        </div>

                        <div style="margin-bottom:22px;">
                            <h2>Recovery</h2>
                            <div class="task-meta" style="white-space:pre-wrap;margin-top:10px;">
                                ${escapeHtml(execution.recovery || "No recovery action recorded.")}
                            </div>
                        </div>

                        <div style="margin-bottom:22px;">
                            <h2>Failure Analysis</h2>
                            <div class="task-meta" style="white-space:pre-wrap;margin-top:10px;">
                                ${escapeHtml(execution.failure_analysis || "No failure analysis recorded.")}
                            </div>
                        </div>

                        <div>
                            <h2>Execution Result</h2>
                            <div class="task-meta" style="white-space:pre-wrap;margin-top:10px;">
                                ${formatExecutionResult(execution.result)}
                            </div>
                        </div>
                    </section>
                `;

                document.getElementById("backToAgentDashboard").addEventListener("click", () => {
                    showAgentMemory(agentKey, agents);
                });

            } catch (error) {
                    alert(`Unable to load execution task: ${error.message}`);
                }
            });
        });

        document.getElementById("memoryCount").textContent =
            `${memories.length} memor${memories.length === 1 ? "y" : "ies"}`;

        const memoryContainer = document.getElementById("agentMemory");

        if (!memories.length) {
            memoryContainer.innerHTML =
                '<div class="task-meta">No memory recorded for this agent yet.</div>';
        } else {
            memoryContainer.innerHTML = memories.slice(0, 10).map(memory => `
                <div class="task" style="margin-bottom:10px;">
                    <div class="task-top">
                        <span class="task-name">
                            ${escapeHtml(memory.task_id || "Agent Memory")}
                        </span>
                    </div>

                    <div class="task-meta" style="white-space:pre-wrap;margin-top:10px;">
                        ${escapeHtml(memory.memory || memory.content || String(memory))}
                    </div>

                    ${
                        memory.created_at
                            ? `<div class="task-meta" style="margin-top:8px;">
                                Created: ${escapeHtml(memory.created_at)}
                               </div>`
                            : ""
                    }
                </div>
            `).join("");
        }
    } catch (error) {
        document.getElementById("agentExecutions").innerHTML =
            `<div class="task-meta">Unable to load agent dashboard: ${escapeHtml(error.message)}</div>`;

        document.getElementById("agentMemory").innerHTML =
            `<div class="task-meta">Unable to load agent memory: ${escapeHtml(error.message)}</div>`;
    }
}

