document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = document.getElementById("btn");
  const err = document.getElementById("error");
  btn.disabled = true;
  err.textContent = "";
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user: document.getElementById("user").value,
        password: document.getElementById("password").value,
      }),
    });
    if (r.ok) {
      location.href = "/";
      return;
    }
    err.textContent = (await r.json()).error || "No se pudo iniciar sesión";
    document.getElementById("password").select();
  } catch {
    err.textContent = "Sin conexión con el servidor";
  }
  btn.disabled = false;
});
