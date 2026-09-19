import { mkdir } from "node:fs/promises";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { join, resolve } from "node:path";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
await mkdir(join(root, "addon/icons"), { recursive: true });
for (const size of [48, 96, 128]) {
  await new Promise((resolve, reject) => {
    const child = spawn("convert", ["-background", "none", join(root, "addon/icons/icon.svg"), "-resize", `${size}x${size}`, `png24:${join(root, `addon/icons/icon-${size}.png`)}`], { stdio: "inherit" });
    child.on("error", reject);
    child.on("exit", (code) => code === 0 ? resolve() : reject(new Error(`convert exited with ${code}`)));
  });
}
console.log("Refreshed addon/icons/icon-48.png, icon-96.png, and icon-128.png");
