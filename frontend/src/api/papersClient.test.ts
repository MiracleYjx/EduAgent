import { afterEach, expect, it, vi } from "vitest";
import { confirmPaper, listQuestions, type Paper } from "./papersClient";
afterEach(()=>vi.unstubAllGlobals());
it("confirms only selected identities with their exact revisions and no Setup token",async()=>{
  const value:Paper={id:"import",subject_id:"subject",document_id:"doc",status:"Pending Review",assets:[],
    questions:[1,2].map(n=>({id:"q"+n,revision:n,status:"Pending Correction",question_type:"short_answer",
      stem_blocks:[{type:"text",text:"Question "+n}],source_locations:[{document_id:"doc",anchor:"p"+n}]}))};
  const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify(value),{status:200}));
  vi.stubGlobal("fetch",fetch);
  await confirmPaper(value,["q2"]);
  const options=fetch.mock.calls[0][1] as RequestInit;
  expect(JSON.parse(String(options.body))).toEqual({question_ids:["q2"],expected_revisions:{q2:2}});
  expect(options.credentials).toBe("omit");
  expect(options.headers).not.toHaveProperty("Authorization");
});
it("keeps an HTTP scope failure visible and never turns it into an empty list",async()=>{
  const fetch=vi.fn().mockResolvedValue(new Response(JSON.stringify({code:"KNOWLEDGE_POINT_NOT_FOUND"}),{status:404}));
  vi.stubGlobal("fetch",fetch);
  await expect(listQuestions("subject",{knowledge_point_ids:"missing"})).rejects.toMatchObject({
    code:"KNOWLEDGE_POINT_NOT_FOUND",status:404,
  });
  expect(fetch).toHaveBeenCalledOnce();
});
