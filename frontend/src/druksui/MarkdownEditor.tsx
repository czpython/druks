import { useEffect, useRef, type CSSProperties, type ReactNode } from 'react'
import {
  Bold,
  Code,
  Italic,
  Link as LinkIcon,
  List,
  ListOrdered,
  Quote,
  Strikethrough,
} from 'lucide-react'
import { Markdown } from '@tiptap/markdown'
import Placeholder from '@tiptap/extension-placeholder'
import { EditorContent, useEditor, useEditorState } from '@tiptap/react'
import { BubbleMenu } from '@tiptap/react/menus'
import StarterKit from '@tiptap/starter-kit'

import type { Field } from '../api/types'

function MarkButton({
  pressed,
  label,
  onClick,
  children,
}: {
  pressed: boolean
  label: string
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={pressed}
      className="dui-markdown-mark"
      onMouseDown={(event) => event.preventDefault()}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

export function MarkdownEditor({
  field,
  id,
  value,
  onChange,
  onBlur,
  describedBy,
  isInvalid,
}: {
  field: Extract<Field, { field: 'text_area' }>
  id: string
  value: string
  onChange: (name: string, value: unknown) => void
  onBlur?: () => void
  describedBy?: string
  isInvalid: boolean
}) {
  const onChangeRef = useRef(onChange)
  const onBlurRef = useRef(onBlur)
  const valueRef = useRef(value)
  useEffect(() => {
    onChangeRef.current = onChange
    onBlurRef.current = onBlur
    valueRef.current = value
  })

  const editor = useEditor({
    immediatelyRender: false,
    extensions: [
      StarterKit.configure({
        underline: false,
        heading: { levels: [1, 2, 3] },
        link: { openOnClick: false },
      }),
      Markdown,
      Placeholder.configure({ placeholder: field.placeholder }),
    ],
    content: value,
    contentType: 'markdown',
    editorProps: {
      attributes: {
        id,
        class: 'dui-markdown-editor markdown-content dui-markdown',
        role: 'textbox',
        'aria-label': field.label,
        'aria-multiline': 'true',
        ...(describedBy ? { 'aria-describedby': describedBy } : {}),
        ...(isInvalid ? { 'aria-invalid': 'true' } : {}),
        ...(field.isRequired ? { 'aria-required': 'true' } : {}),
      },
      handleDOMEvents: {
        blur: () => {
          onBlurRef.current?.()
          return false
        },
      },
    },
    onUpdate: ({ editor: current }) => {
      const markdown = current.getMarkdown()
      if (markdown.trim() === valueRef.current.trim()) return
      onChangeRef.current(field.name, markdown)
    },
  })

  const marks = useEditorState({
    editor,
    selector: ({ editor: current }) => {
      if (!current) return null
      return {
        bold: current.isActive('bold'),
        italic: current.isActive('italic'),
        strike: current.isActive('strike'),
        code: current.isActive('code'),
        link: current.isActive('link'),
        bullet: current.isActive('bulletList'),
        ordered: current.isActive('orderedList'),
        quote: current.isActive('blockquote'),
        block: current.isActive('heading', { level: 1 })
          ? '1'
          : current.isActive('heading', { level: 2 })
            ? '2'
            : current.isActive('heading', { level: 3 })
              ? '3'
              : 'p',
      }
    },
  })

  return (
    <div
      className="dui-input dui-markdown-field"
      style={{ '--dui-markdown-rows': field.rows } as CSSProperties}
    >
      {editor && marks ? (
        <BubbleMenu
          editor={editor}
          className="dui-markdown-bubble"
          options={{ placement: 'top', offset: 8 }}
          shouldShow={({ editor: current, from, to }) =>
            current.isEditable && from !== to && !current.isActive('codeBlock')
          }
        >
          <MarkButton
            pressed={marks.bold}
            label="Bold"
            onClick={() => editor.chain().focus().toggleBold().run()}
          >
            <Bold size={15} strokeWidth={1.8} />
          </MarkButton>
          <MarkButton
            pressed={marks.italic}
            label="Italic"
            onClick={() => editor.chain().focus().toggleItalic().run()}
          >
            <Italic size={15} strokeWidth={1.8} />
          </MarkButton>
          <MarkButton
            pressed={marks.strike}
            label="Strikethrough"
            onClick={() => editor.chain().focus().toggleStrike().run()}
          >
            <Strikethrough size={15} strokeWidth={1.8} />
          </MarkButton>
          <MarkButton
            pressed={marks.code}
            label="Code"
            onClick={() => editor.chain().focus().toggleCode().run()}
          >
            <Code size={15} strokeWidth={1.8} />
          </MarkButton>
          <MarkButton
            pressed={marks.link}
            label="Link"
            onClick={() => {
              if (editor.isActive('link')) {
                editor.chain().focus().unsetLink().run()
                return
              }
              const href = window.prompt('Link URL', editor.getAttributes('link').href ?? 'https://')
              if (href === null) return
              const next = href.trim()
              if (!next) {
                editor.chain().focus().unsetLink().run()
                return
              }
              editor.chain().focus().extendMarkRange('link').setLink({ href: next }).run()
            }}
          >
            <LinkIcon size={15} strokeWidth={1.8} />
          </MarkButton>
          <span className="dui-markdown-bubble-rule" />
          <select
            aria-label="Text style"
            className="dui-markdown-block"
            value={marks.block}
            onChange={(event) => {
              const next = event.target.value
              const chain = editor.chain().focus()
              if (next === 'p') chain.setParagraph().run()
              else chain.setHeading({ level: Number(next) as 1 | 2 | 3 }).run()
            }}
          >
            <option value="p">Text</option>
            <option value="1">Heading 1</option>
            <option value="2">Heading 2</option>
            <option value="3">Heading 3</option>
          </select>
          <span className="dui-markdown-bubble-rule" />
          <MarkButton
            pressed={marks.bullet}
            label="Bullet list"
            onClick={() => editor.chain().focus().toggleBulletList().run()}
          >
            <List size={15} strokeWidth={1.8} />
          </MarkButton>
          <MarkButton
            pressed={marks.ordered}
            label="Numbered list"
            onClick={() => editor.chain().focus().toggleOrderedList().run()}
          >
            <ListOrdered size={15} strokeWidth={1.8} />
          </MarkButton>
          <MarkButton
            pressed={marks.quote}
            label="Quote"
            onClick={() => editor.chain().focus().toggleBlockquote().run()}
          >
            <Quote size={15} strokeWidth={1.8} />
          </MarkButton>
        </BubbleMenu>
      ) : null}
      <EditorContent editor={editor} />
    </div>
  )
}
