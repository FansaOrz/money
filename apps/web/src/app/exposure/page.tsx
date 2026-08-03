import { redirect } from "next/navigation";

/** 穿透分析已并入 /positions?view=exposure，此路由仅保留直链重定向。 */
export default function ExposurePage() {
  redirect("/positions?view=exposure");
}
