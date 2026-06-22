//! OXC-backed source transform guest.
//!
//! ABI shape:
//! - Host writes `name` and `source` into linear memory with `qjst_alloc`.
//! - Host calls `qjst_transform(...)`.
//! - On OK, transformed source is available via `qjst_result_ptr/len`.
//! - On error, a diagnostic string is available via `qjst_error_ptr/len`.
//! - Host copies the active slot and calls `qjst_result_free`.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;

use oxc_allocator::Allocator;
use oxc_ast::ast::{Declaration, Program, Statement, VariableDeclarationKind};
use oxc_codegen::{Codegen, CodegenOptions};
use oxc_parser::Parser;
use oxc_semantic::SemanticBuilder;
use oxc_span::SourceType;
use oxc_transformer::{TransformOptions, Transformer};

mod mem;

const STATUS_OK: i32 = 0;
const STATUS_UNCHANGED: i32 = 1;
const STATUS_BAD_INPUT: i32 = 2;
const STATUS_PARSE_ERROR: i32 = 3;
const STATUS_TRANSFORM_ERROR: i32 = 4;
const STATUS_PANIC: i32 = 5;

const FLAG_SOURCE_TS: u32 = 1 << 0;
const FLAG_SOURCE_TSX: u32 = 1 << 1;
const FLAG_STRIP_TYPESCRIPT: u32 = 1 << 8;
const FLAG_TOP_LEVEL_CONST_TO_VAR: u32 = 1 << 9;
const FLAG_SOURCE_MASK: u32 = FLAG_SOURCE_TS | FLAG_SOURCE_TSX;
const FLAG_PASS_MASK: u32 = FLAG_STRIP_TYPESCRIPT | FLAG_TOP_LEVEL_CONST_TO_VAR;

#[no_mangle]
pub extern "C" fn qjst_transform(
    name_ptr: *const u8,
    name_len: usize,
    source_ptr: *const u8,
    source_len: usize,
    flags: u32,
) -> i32 {
    mem::qjst_result_free();
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        let name = match mem::read_utf8(name_ptr, name_len) {
            Some(name) => name,
            None => return Err(TransformFailure::bad_input("invalid module name")),
        };
        let source = match mem::read_utf8(source_ptr, source_len) {
            Some(source) => source,
            None => return Err(TransformFailure::bad_input("invalid module source")),
        };
        transform_module_source(name, source, flags)
    }));

    match outcome {
        Ok(Ok(Some(code))) => {
            mem::set_result(code.into_bytes());
            STATUS_OK
        }
        Ok(Ok(None)) => STATUS_UNCHANGED,
        Ok(Err(err)) => {
            mem::set_error(err.message.into_bytes());
            err.status
        }
        Err(_) => {
            mem::set_error(b"transform guest panicked".to_vec());
            STATUS_PANIC
        }
    }
}

struct TransformFailure {
    status: i32,
    message: String,
}

impl TransformFailure {
    fn bad_input(message: impl Into<String>) -> Self {
        Self {
            status: STATUS_BAD_INPUT,
            message: message.into(),
        }
    }

    fn parse(message: impl Into<String>) -> Self {
        Self {
            status: STATUS_PARSE_ERROR,
            message: message.into(),
        }
    }

    fn transform(message: impl Into<String>) -> Self {
        Self {
            status: STATUS_TRANSFORM_ERROR,
            message: message.into(),
        }
    }
}

fn transform_module_source(
    name: &str,
    source: &str,
    flags: u32,
) -> Result<Option<String>, TransformFailure> {
    let Some(config) = TransformConfig::from_flags(flags)? else {
        return Ok(None);
    };

    let allocator = Allocator::default();
    let parser_return = Parser::new(&allocator, source, config.source_type).parse();
    if !parser_return.diagnostics.is_empty() {
        return Err(TransformFailure::parse(format_diagnostics(
            "source parse error",
            name,
            parser_return.diagnostics,
        )));
    }

    let mut program = parser_return.program;
    if config.strip_typescript {
        run_typescript_transform(&allocator, name, &mut program)?;
    }
    if config.top_level_const_to_var {
        rewrite_top_level_const_to_var(&mut program);
    }

    Ok(Some(codegen(&program)))
}

struct TransformConfig {
    source_type: SourceType,
    strip_typescript: bool,
    top_level_const_to_var: bool,
}

impl TransformConfig {
    fn from_flags(flags: u32) -> Result<Option<Self>, TransformFailure> {
        if flags & FLAG_PASS_MASK == 0 {
            return Ok(None);
        }

        let source_flags = flags & FLAG_SOURCE_MASK;
        if source_flags == FLAG_SOURCE_MASK {
            return Err(TransformFailure::bad_input(
                "source kind cannot be both TypeScript and TSX",
            ));
        }

        let strip_typescript = flags & FLAG_STRIP_TYPESCRIPT != 0;
        let top_level_const_to_var = flags & FLAG_TOP_LEVEL_CONST_TO_VAR != 0;

        if strip_typescript && source_flags == 0 {
            return Err(TransformFailure::bad_input(
                "TypeScript stripping requires a TypeScript source kind",
            ));
        }

        let source_type = if source_flags == FLAG_SOURCE_TSX {
            SourceType::tsx().with_module(true)
        } else if source_flags == FLAG_SOURCE_TS {
            SourceType::ts().with_module(true)
        } else {
            SourceType::mjs()
        };

        Ok(Some(Self {
            source_type,
            strip_typescript,
            top_level_const_to_var,
        }))
    }
}

fn run_typescript_transform<'a>(
    allocator: &'a Allocator,
    name: &str,
    program: &mut Program<'a>,
) -> Result<(), TransformFailure> {
    let semantic_return = SemanticBuilder::new()
        .with_excess_capacity(2.0)
        .with_enum_eval(true)
        .build(program);
    let scoping = semantic_return.semantic.into_scoping();
    let options = TransformOptions::default();
    let transform_return =
        Transformer::new(allocator, Path::new(name), &options).build_with_scoping(scoping, program);
    if !transform_return.diagnostics.is_empty() {
        return Err(TransformFailure::transform(format_diagnostics(
            "TypeScript transform error",
            name,
            transform_return.diagnostics,
        )));
    }
    Ok(())
}

fn rewrite_top_level_const_to_var(program: &mut Program<'_>) {
    for statement in &mut program.body {
        match statement {
            Statement::VariableDeclaration(declaration) => {
                rewrite_const_declaration_kind(&mut declaration.kind);
            }
            Statement::ExportNamedDeclaration(declaration) => {
                if let Some(Declaration::VariableDeclaration(variable)) =
                    declaration.declaration.as_mut()
                {
                    rewrite_const_declaration_kind(&mut variable.kind);
                }
            }
            _ => {}
        }
    }
}

fn rewrite_const_declaration_kind(kind: &mut VariableDeclarationKind) {
    if *kind == VariableDeclarationKind::Const {
        *kind = VariableDeclarationKind::Var;
    }
}

fn codegen(program: &Program<'_>) -> String {
    Codegen::new()
        .with_options(CodegenOptions::default())
        .build(program)
        .code
}

fn format_diagnostics<T>(prefix: &str, name: &str, diagnostics: T) -> String
where
    T: IntoIterator,
    T::Item: std::fmt::Display,
{
    let messages: Vec<String> = diagnostics
        .into_iter()
        .map(|diagnostic| diagnostic.to_string())
        .collect();
    if messages.is_empty() {
        format!("{prefix} in {name}")
    } else {
        format!("{prefix} in {name}: {}", messages.join("; "))
    }
}
