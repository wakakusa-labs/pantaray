# Third-party notices

The released Pantaray app bundles the components below, in addition to the Node packages
resolved by `frontend/pnpm-lock.yaml` and the Python packages resolved by `agents/uv.lock`.
License texts that have to travel with the binary are reproduced here.

| Component | Version | Pinned by |
| --- | --- | --- |
| [Electron](https://github.com/electron/electron) | 44.10.0 | `frontend/package.json` |
| [Zanei](https://github.com/KentoShimizu/zanei) | 0.7.0 | `frontend/electron/zanei_release.json` |
| [sqlite-vec](https://github.com/asg017/sqlite-vec) | 0.1.9 | `agents/pyproject.toml` |
| [PDFium](https://pdfium.googlesource.com/pdfium/) (via [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)) | 5.13.0 (PDFium 153.0.7999.0) | `agents/pyproject.toml` |
| [bekko-embedding-v1-a25m](https://huggingface.co/hotchpotch/bekko-embedding-v1-a25m) | revision `44f0b8af` | `frontend/electron/embedding_model_release.json` |
| [Sora](https://github.com/sora-xor/sora-font) (subset) | — | `frontend/src/assets/fonts/pantaray-wordmark/` |
| [CPython](https://github.com/astral-sh/python-build-standalone) | 3.12.13 (build 20260325) | `frontend/scripts/prepare-local-backend-helper-runtime.js` |

## Electron

The desktop application runs on Electron, which is distributed under the MIT license:

```
Copyright (c) Electron contributors
Copyright (c) 2013-2020 GitHub Inc.

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## Zanei

Activity recording is performed by the Zanei recorder, a separate binary that Pantaray starts
and reads. The release tarball is fetched at build time and pinned by SHA-256, and the recorder
is installed at `Pantaray.app/Contents/Resources/zanei/bin/zanei`. Zanei is dual-licensed under
MIT or Apache-2.0, at your option.

Zanei's own third-party notices ship next to the binary, at
`Pantaray.app/Contents/Resources/zanei/THIRD_PARTY_NOTICES.md`.

`frontend/tests/fixtures/zanei_privacy_parity_cases.json` is a verbatim copy of Zanei's
`crates/zanei-core/tests/privacy_parity_cases.json` at the pinned release, under the same license.
It is test data and does not ship in the app.

## sqlite-vec

Vector search over the local SQLite store uses the sqlite-vec extension, which is distributed
under the MIT license:

```
MIT License

Copyright (c) 2024 Alex Garcia

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## PDFium

Drawing a PDF page as an image uses PDFium, the PDF engine from Chrome, reached through the
`pypdfium2` package. The package carries a prebuilt `libpdfium.dylib` produced by
[pdfium-binaries](https://github.com/bblanchon/pdfium-binaries), installed at
`Pantaray.app/Contents/Resources/local_backend_helper/lib/python3.12/site-packages/pypdfium2_raw/`.

PDFium is distributed under the 3-clause BSD license:

```
Copyright 2014 The PDFium Authors

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google Inc. nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

The `pypdfium2` bindings are offered under `Apache-2.0 OR BSD-3-Clause`, and are taken here
under the 3-clause BSD license reproduced above, with the copyright held by geisserml
(`SPDX-FileCopyrightText: 2026 geisserml <geisserml@gmail.com>`). The `pdfium-binaries` build
scripts are MIT, Copyright 2014-2025 Benoit Blanchon.

PDFium links further libraries into that binary — among them FreeType, libjpeg-turbo, libpng,
zlib, ICU, Little CMS, OpenJPEG, libtiff, Abseil, AGG and simdutf. Their notices are not
reproduced here: the package ships them next to the code that carries them, and they are
installed with it at
`Pantaray.app/Contents/Resources/local_backend_helper/lib/python3.12/site-packages/pypdfium2-5.13.0.dist-info/licenses/`,
where `LICENSES/` holds the bindings' own license texts and
`data/darwin_arm64/BUILD_LICENSES/` holds one file per library compiled into `libpdfium.dylib`.

## bekko-embedding-v1-a25m

Semantic memory search embeds text on the machine, with the
`hotchpotch/bekko-embedding-v1-a25m` model published on Hugging Face. The build fetches the
model's own arm64 int8 ONNX graph and its tokenizer at the revision pinned in
`frontend/electron/embedding_model_release.json`, verifies both against the SHA-256 digests
pinned there, and installs them at `Pantaray.app/Contents/Resources/local_embedding_model/`.
The files are the published ones unchanged, and nothing is fetched at run time.

At that revision the model repository carries no `LICENSE` file and no copyright notice of its
own; its model card declares `license: mit` and names Yuichi Tateno (@hotchpotch) as the author:
<https://huggingface.co/hotchpotch/bekko-embedding-v1-a25m/blob/44f0b8af0f487acd0ccf1a7cb7ae7a29a6dfc09c/README.md>

Its backbone is [mmBERT-L13H384-pruned](https://huggingface.co/hotchpotch/mmBERT-L13H384-pruned)
(MIT), pruned from [mmBERT-small](https://huggingface.co/jhu-clsp/mmBERT-small) (MIT), which is
described in "mmBERT: A Modern Multilingual Encoder with Annealed Language Learning"
(arXiv:2509.06888). The mmBERT model card states that its tokenizer is the Gemma 2 tokenizer.

The MIT license reads:

```
MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Sora

The Pantaray wordmark is set in a subset of the Sora typeface, which is distributed under the
SIL Open Font License, Version 1.1:

```
Copyright 2019 The Sora Project Authors (https://github.com/sora-xor/sora-font)

This Font Software is licensed under the SIL Open Font License, Version 1.1.

This license is copied below, and is also available with a FAQ at: https://scripts.sil.org/OFL


-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```

## CPython and the Python dependency tree

The local runtime runs on a relocatable CPython 3.12.13 build taken from release `20260325` of
[astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone). The
archive is pinned by SHA-256 at build time and installed, together with the Python packages in
`agents/uv.lock`, at `Pantaray.app/Contents/Resources/local_backend_helper`.

That interpreter build, the libraries compiled into it, and the bundled Python packages are each
distributed under their own licenses, which are not reproduced in this file.
