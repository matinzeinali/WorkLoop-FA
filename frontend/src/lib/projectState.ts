import type { ProjectInfo } from "./workloopApi";

const ACTIVE_PROJECT_KEY = "workloop:active-project";
export const ACTIVE_PROJECT_CHANGED = "workloop:active-project-changed";

export function getActiveProject(): ProjectInfo | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(ACTIVE_PROJECT_KEY) ?? "null");
    if (!value || typeof value !== "object") return null;
    const project = value as Partial<ProjectInfo>;
    if (typeof project.path !== "string" || !project.path) return null;
    return {
      path: project.path,
      name: typeof project.name === "string" ? project.name : null,
      exists: project.exists !== false,
      file_count: typeof project.file_count === "number" ? project.file_count : null,
    };
  } catch {
    return null;
  }
}

export function setActiveProject(project: ProjectInfo | null) {
  try {
    if (project?.path) localStorage.setItem(ACTIVE_PROJECT_KEY, JSON.stringify(project));
    else localStorage.removeItem(ACTIVE_PROJECT_KEY);
  } catch { /* Keep project selection usable when storage is unavailable. */ }
  window.dispatchEvent(new CustomEvent<ProjectInfo | null>(ACTIVE_PROJECT_CHANGED, { detail: project }));
}
