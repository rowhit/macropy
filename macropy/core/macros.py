"""The main source of all things MacroPy"""

import sys
import imp
import ast
import itertools
from ast import *
from util import *
from walkers import *
from misc import *


@singleton
class hygienic_self_ref:
    pass


class MacroFunction(object):
    """Wraps a macro-function, to provide nicer error-messages in the common
    case where the macro is imported but macro-expansion isn't triggered"""
    def __init__(self, func):
        self.func = func

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def __getitem__(self, i):
        raise TypeError(
            "Macro `%s` illegally invoked at runtime; did you import it "
            "properly using `from ... import macros, %s`?"
            % (self.func.func_name, self.func.func_name)
        )


class Macros(object):
    """A registry of macros belonging to a module; used via

    ```python
    macros = Macros()

    @macros.expr
    def my_macro(tree):
        ...
    ```

    Where the decorators are used to register functions as macros belonging
    to that module.
    """

    class Registry(object):
        def __init__(self, wrap = lambda x: x):
            self.registry = {}
            self.wrap = wrap

        def __call__(self, f, name=None):

            if name is not None:
                self.registry[name] = self.wrap(f)
            if hasattr(f, "func_name"):
                self.registry[f.func_name] = self.wrap(f)
            if hasattr(f, "__name__"):
                self.registry[f.__name__] = self.wrap(f)

            return self.wrap(f)

    def __init__(self):
        # Different kinds of macros
        self.expr = Macros.Registry(MacroFunction)
        self.block = Macros.Registry(MacroFunction)
        self.decorator = Macros.Registry(MacroFunction)

        self.expose_unhygienic = Macros.Registry()


# Global filters, for other modules to hook into MacroPy's workflow while
# keeping this module itself unaware of their presence.
injected_vars = []      # functions to inject values throughout each files macros
filters = []            # functions to call on every macro-expanded snippet
post_processing = []    # functions to call on every macro-expanded file

import gen_sym as x
def expand_entire_ast(tree, src, bindings):

    def call(thing, **kw):
        return thing(
            exact_src=lambda t: exact_src(t, src, indexes, line_lengths),
            expand_macros=lambda t: expand_ast(t),
            **kw
        )

    file_vars = {v.func_name: call(v, tree=tree) for v in injected_vars}

    # you don't pay for what you don't use
    positions = Lazy(lambda: indexer.collect(tree))
    line_lengths = Lazy(lambda: map(len, src.split("\n")))
    indexes = Lazy(lambda: distinct([linear_index(line_lengths(), l, c) for (l, c) in positions()] + [len(src)]))

    allnames = [(m, name, asname) for m, names in bindings for name, asname in names]

    def extract_macros(pick_registry):
        return {
            asname: (registry[name], ma)
            for ma, name, asname in allnames
            for registry in [pick_registry(ma.macros).registry]
            if name in registry.keys()
        }
    block_registry = extract_macros(lambda x: x.block)
    expr_registry = extract_macros(lambda x: x.expr)
    decorator_registry = extract_macros(lambda x: x.decorator)


    def expand_ast(tree):
        """Go through an AST, hunting for macro invocations and expanding any that
        are found"""

        def expand_if_in_registry(macro_tree, body_tree, args, registry, **kwargs):
            """check if `tree` is a macro in `registry`, and if so use it to expand `args`"""
            if isinstance(macro_tree, Name) and macro_tree.id in registry:

                (the_macro, the_module) = registry[macro_tree.id]
                new_tree = call(the_macro,
                    tree=body_tree,
                    args=args,
                    **dict(kwargs.items() + file_vars.items())
                )
                for filter in filters:

                    new_tree = call(filter,
                        tree=new_tree,
                        args=args,
                        **dict(kwargs.items() + file_vars.items())
                    )

                new_tree = ast_ctx_fixer.recurse(new_tree, Load())
                fill_line_numbers(new_tree, macro_tree.lineno, macro_tree.col_offset)
                return new_tree
            elif isinstance(macro_tree, Call):
                args.extend(macro_tree.args)
                return expand_if_in_registry(macro_tree.func, body_tree, args, registry)

        def preserve_line_numbers(func):
            """Decorates a tree-transformer function to stick the original line
            numbers onto the transformed tree"""
            def run(tree):
                pos = (tree.lineno, tree.col_offset) if hasattr(tree, "lineno") and hasattr(tree, "col_offset") else None
                new_tree = func(tree)

                if pos:
                    t = new_tree
                    while type(t) is list:
                        t = t[0]


                    (t.lineno, t.col_offset) = pos
                return new_tree
            return run

        @preserve_line_numbers
        def macro_expand(tree):
            """Tail Recursively expands all macros in a single AST node"""
            if isinstance(tree, With):
                assert isinstance(tree.body, list), real_repr(tree.body)
                new_tree = expand_if_in_registry(tree.context_expr, tree.body, [], block_registry, target=tree.optional_vars)

                if new_tree:
                    assert isinstance(new_tree, list), type(new_tree)
                    return macro_expand(new_tree)

            if isinstance(tree, Subscript) and type(tree.slice) is Index:

                new_tree = expand_if_in_registry(tree.value, tree.slice.value, [], expr_registry)

                if new_tree:
                    assert isinstance(new_tree, expr), type(new_tree)
                    return macro_expand(new_tree)


            if isinstance(tree, ClassDef) or isinstance(tree, FunctionDef):
                seen_decs = []
                additions = []
                while tree.decorator_list != []:
                    dec = tree.decorator_list[0]
                    tree.decorator_list = tree.decorator_list[1:]

                    new_tree = expand_if_in_registry(dec, tree, [], decorator_registry)

                    if new_tree is None:
                        seen_decs.append(dec)
                    else:
                        tree = new_tree
                        tree = macro_expand(tree)
                        if type(tree) is list:
                            additions = tree[1:]
                            tree = tree[0]

                tree.decorator_list = seen_decs
                if len(additions) == 0:
                    return tree
                else:
                    return [tree] + additions

            return tree

        @Walker
        def macro_searcher(tree, **kw):
            x = macro_expand(tree)
            return x

        tree = macro_searcher.recurse(tree)

        return tree
    tree = expand_ast(tree)
    for post in post_processing:
        tree = call(post,
            tree=tree,
            **file_vars
        )


    return tree


def detect_macros(tree):
    """Look for macros imports within an AST, transforming them and extracting
    the list of macro modules."""
    bindings = []


    for stmt in tree.body:
        if isinstance(stmt, ImportFrom) \
                and stmt.names[0].name == 'macros' \
                and stmt.names[0].asname is None:
            __import__(stmt.module)
            mod = sys.modules[stmt.module]

            bindings.append((
                stmt.module,
                [(t.name, t.asname or t.name) for t in stmt.names[1:]]
            ))

            stmt.names = [
                name for name in stmt.names
                if name.name not in mod.macros.block.registry
                if name.name not in mod.macros.expr.registry
                if name.name not in mod.macros.decorator.registry
            ]

            stmt.names.extend([
                alias(x, x) for x in
                mod.macros.expose_unhygienic.registry.keys()
            ])

    return bindings

def check_annotated(tree):
    """Shorthand for checking if an AST is of the form something[...]"""
    if isinstance(tree, Subscript) and \
                    type(tree.slice) is Index and \
                    type(tree.value) is Name:
        return tree.value.id, tree.slice.value


