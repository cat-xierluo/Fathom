#!/usr/bin/awk -f
# yaml-flatten.awk — 把 doc-curator 的 yaml 子集 flat 化为 path=value 行。
#
# 输入：yaml 文件（stdin 或文件参数）；输出：每行一个 path=value，path 用 . 分隔。
# 支持结构：顶层 map key、嵌套段、list item（- key: val / - scalar）、标量字段。
# 不支持：flow 数组 [a,b]、多行字符串、锚点/引用 —— doc-curator config 不用这些，
# 故 config 设计时刻意保持标量字段 + 独立 bool/enum 字段（见 template.yaml 注释）。
#
# 示例：
#   files:               → files.1.path=docs/TASKS.md
#     - path: docs/...          files.1.role=active-tasks
#       role: active-tasks
#   context_sync:        → context_sync.local_reversible.max_files=1
#     local_reversible:         context_sync.change_types.1.pattern=manuscript/**
#       max_files: 1
#     change_types:
#       - pattern: "manuscript/**"
#
# 算法：缩进栈。非 item 行弹栈条件 stack_spaces[depth] >= 当前行 spaces（兄弟/父级回收）；
# section(map key) 与 list item 入栈时记录「行缩进」与「条目类型」；标量字段不入栈，归属栈顶路径。
# item 行例外：与 section key 同列的 "- item" 是合法 YAML，此时同缩进的 key 是父级，不得弹出。

function cleanval(s,   t) {
    t = s
    gsub(/^[ ]+/, "", t)
    gsub(/[ ]+$/, "", t)
    gsub(/^"/, "", t)   # 去前引号
    gsub(/"$/, "", t)   # 去尾引号
    return t
}

BEGIN { depth = 0 }

{
    line = $0
    sub(/\r$/, "", line)                       # 兼容 CRLF
    if (line ~ /^[[:space:]]*$/) next           # 空行
    if (line ~ /^[[:space:]]*#/) next           # 注释行

    # 算前导空格数（规范用空格，非 Tab）
    spaces = 0
    ln = length(line)
    for (i = 1; i <= ln && substr(line, i, 1) == " "; i++) spaces++
    content = substr(line, spaces + 1)
    if (substr(content, 1, 1) == "\t") next     # Tab 缩进不规范，跳过
    handled = 0

    # 弹栈：回收同缩进或更浅的旧条目。item 行只回收同缩进的兄弟 item 与更浅的 key，
    # 保留同缩进的父级 key（旧实现统一 >= 弹栈，会把父级弹掉，item 路径逃逸为顶层，
    # cfg_list_field 读列表字段取空）。
    is_item = (content ~ /^-[[:space:]]/)
    if (is_item) {
        while (depth > 0) {
            if (stack_kind[depth] == "item") {
                if (stack_spaces[depth] >= spaces) { depth--; continue }
                break
            }
            if (stack_spaces[depth] > spaces) { depth--; continue }
            break
        }
    } else {
        while (depth > 0 && stack_spaces[depth] >= spaces) depth--
    }
    parent_path = (depth > 0) ? stack_path[depth] : ""

    if (is_item) {
        handled = 1
        # list item；dash 后允许多空白，先剥掉再解析字段（否则 "-  key: v" 退化成整行标量值）
        item_content = substr(content, 2)
        sub(/^[[:space:]]+/, "", item_content)
        idx = ++list_count[parent_path]
        item_path = (parent_path == "") ? idx : (parent_path "." idx)
        if (item_content ~ /^[A-Za-z_][A-Za-z0-9_]*:/) {
            k = item_content; sub(/:.*/, "", k)
            v = item_content; sub(/^[A-Za-z_][A-Za-z0-9_]*:[ ]*/, "", v)
            print item_path "." k "=" cleanval(v)
        } else {
            print item_path "=" cleanval(item_content)
        }
        depth++
        stack_spaces[depth] = spaces
        stack_path[depth] = item_path
        stack_kind[depth] = "item"
    } else if (content ~ /^[A-Za-z_][A-Za-z0-9_]*:$/) {
        handled = 1
        # map key without value（段标题）
        key = content; sub(/:$/, "", key)
        new_path = (parent_path == "") ? key : (parent_path "." key)
        depth++
        stack_spaces[depth] = spaces
        stack_path[depth] = new_path
        stack_kind[depth] = "key"
    } else if (content ~ /^[A-Za-z_][A-Za-z0-9_]*:/) {
        handled = 1
        # scalar key: value
        key = content; sub(/:.*/, "", key)
        v = content; sub(/^[A-Za-z_][A-Za-z0-9_]*:[ ]*/, "", v)
        scalar_path = (parent_path == "") ? key : (parent_path "." key)
        print scalar_path "=" cleanval(v)
    }
    if (!handled) {
        print "yaml-flatten.awk: unsupported syntax at line " NR > "/dev/stderr"
        invalid = 1
    }
}

END { if (invalid) exit 2 }
