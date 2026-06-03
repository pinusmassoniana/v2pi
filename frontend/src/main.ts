import "./app.css";
import { mount } from "svelte";
import App from "./App.svelte";
import { applyTheme, resolveInitialTheme, getStoredTheme, systemTheme } from "./lib/theme";

// Reconcile the theme the inline (anti-FOUC) script set, and persist a default
// on first run so the toggle has a concrete starting value.
applyTheme(resolveInitialTheme(getStoredTheme(), systemTheme()));

const app = mount(App, { target: document.getElementById("app")! });
export default app;
