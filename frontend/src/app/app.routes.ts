import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: 'incidents',
    loadComponent: () =>
      import('./features/incidents/incident-list.component').then(
        (module) => module.IncidentListComponent,
      ),
  },
  {
    path: 'incidents/:id',
    loadComponent: () =>
      import('./features/incidents/incident-detail.component').then(
        (module) => module.IncidentDetailComponent,
      ),
  },
  { path: '', pathMatch: 'full', redirectTo: 'incidents' },
  { path: '**', redirectTo: 'incidents' },
];
