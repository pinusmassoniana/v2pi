import "./app.css";
import { mount } from "svelte";
import App from "./App.svelte";
import { applyTheme, resolveInitialTheme, getStoredTheme } from "./lib/theme";

// Reconcile the theme the inline (anti-FOUC) script set, and persist a default
// on first run so the toggle has a concrete starting value. Dark is the primary
// theme — first run defaults to dark, not the system preference.
applyTheme(resolveInitialTheme(getStoredTheme(), "dark"));

const app = mount(App, { target: document.getElementById("app")! });
export default app;
