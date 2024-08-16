# Multilingual Spellchecker for Zim

This is a plugin for the [Zim Desktop Wiki](https://zim-wiki.org/) that adds a multilingual spellchecker to the editor. It uses [enchant](https://abiword.github.io/enchant/) as the backend.

See this [discussion](https://github.com/zim-desktop-wiki/zim-desktop-wiki/discussions/2188) for my motivation to create this plugin.

# Dependencies

Nothing other than enchant and the Python bindings. On Debian it is provided by the `python3-enchant` package.

# Installation

```bash
mkdir -p ~/.local/share/zim/plugins
cd ~/.local/share/zim/plugins
git clone https://github.com/Crissium/zim-multilingual-spellchecker
```

Disable the built in spellchecker, then enable the plugin in Zim and set the default languages in 'Properties' as a whitespace separated list of language codes, e.g. `en_GB fr_FR`.

# Credits

Much of the code is adapted from the original spellchecker plugin by Jaap Karssenberg and [pygtkspellcheck](https://github.com/koehlma/pygtkspellcheck).


# Note

This programme removes diacritics before sorting the suggestions, so, for example é, è, ê, ẽ, ë all come after d and before f. I know in some languages those 'special letters' should be placed after the 'regular letters,' but, well, implementing different sorting logic for each language is too much work. 

This plugin was written in a matter of hours, by pasting large chunks of code over from the two repos mentioned above. I didn't even bother to get rid of the Adapter pattern that is used to reconcile API differences between different spellcheckers. So, expect problems, and report them here if you find any, though I might not be able to fix it quickly enough…

By the way, developing GTK applications with Python isn't easy at all! I have to look up docs for C and there's no completion in VS Code.
